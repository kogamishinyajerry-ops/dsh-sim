"""运行服务：authorizeRuns / submitRuns / getRun / cancelRun / retryRun。

强约束（定义书 §工程API：业务操作）：
- authorizeRuns 仅人工会话；Agent Bearer 始终拒绝（403 + FORBIDDEN）。
- submitRuns 只能使用仍有效的授权；准备摘要变化 409 拒绝。
- 提交原子操作：输入授权校验、Run 集合建立、待执行 Job 建立、幂等记录一次提交。
- 补算生成新 attempt；校验原因、策略与剩余预算；每 Run 最多一个未核实结束 attempt。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from dsh_sim.db.guards import assert_run_matrix_cell_free, assert_single_open_attempt
from dsh_sim.db.models import (
    AttemptRow,
    AuthorizationRow,
    EventRow,
    HumanConfirmationRow,
    PreparationRow,
    RunRow,
    TaskRevisionRow,
    utcnow,
)
from dsh_sim.domain.errors import ApiError, ErrorCode
from dsh_sim.domain.identity import Identity, Role
from dsh_sim.domain.schemas import Attempt, Authorization, Event, Run
from dsh_sim.domain.states import (
    EXECUTION_TERMINAL,
    ApplicabilityState,
    ExecutionState,
    NumericalState,
    TaskFlowState,
)
from dsh_sim.queue.service import enqueue, find_active_job_for_run, request_cancel

from dsh_sim.api.services.task_service import get_task_row, transition_task_flow

CONFIRMATION_TTL_SECONDS = 300  # 人工确认凭据短期有效（5 分钟）


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:24]}"


def issue_confirmation(
    session: Session,
    identity: Identity,
    *,
    action: str,
    target_id: str,
    target_digest: str,
) -> tuple[str, datetime]:
    """createHumanConfirmation：受信 UI 会话发起；单次消费、短期有效；不向模型泄露。"""
    identity.require_human()  # Agent Bearer 始终拒绝
    confirmation_id = _new_id("conf")
    expires_at = datetime.now(timezone.utc).timestamp() + CONFIRMATION_TTL_SECONDS
    row = HumanConfirmationRow(
        confirmation_id=confirmation_id,
        subject_id=identity.subject_id,
        action=action,
        target_id=target_id,
        target_digest=target_digest,
        expires_at=datetime.fromtimestamp(expires_at, timezone.utc),
    )
    session.add(row)
    session.flush()
    return confirmation_id, row.expires_at


def consume_confirmation(
    session: Session,
    identity: Identity,
    *,
    confirmation_id: str,
    action: str,
    target_id: str,
    target_digest: str,
) -> None:
    """校验并单次消费人工确认凭据：绑定 action/target/digest、近期有效、本人。"""
    row = session.get(HumanConfirmationRow, confirmation_id)
    now = utcnow()
    expires_at = row.expires_at if row is not None else None
    if expires_at is not None and expires_at.tzinfo is None:
        # SQLite 读回的 datetime 不带 tz；按 UTC 解释（PG timestamptz 不受影响）
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if (
        row is None
        or row.consumed_at is not None
        or expires_at < now
        or row.subject_id != identity.subject_id
        or row.action != action
        or row.target_id != target_id
        or row.target_digest != target_digest
    ):
        raise ApiError(
            ErrorCode.FORBIDDEN,
            "人工确认凭据无效/已消费/已过期/绑定不匹配",
            details={"action": action, "target_id": target_id},
        )
    row.consumed_at = now
    session.flush()


def authorize_runs(
    session: Session,
    identity: Identity,
    task_id: str,
    *,
    revision: int,
    preparation_id: str,
    prepared_digest: str,
    execution_budget: dict[str, Any],
    confirmation_id: str,
) -> Authorization:
    task = get_task_row(session, identity, task_id)

    # 强约束：Agent Bearer 始终拒绝（定义书 §authorizeRuns）
    identity.require_human()
    if not identity.has_role(Role.EXECUTOR):
        raise ApiError(ErrorCode.FORBIDDEN, "authorizeRuns 需要 EXECUTOR 角色")

    prep = session.get(PreparationRow, preparation_id)
    if prep is None or prep.task_id != task_id:
        raise ApiError(
            ErrorCode.VALIDATION, "准备不存在或不属于该任务", details={"preparation_id": preparation_id}
        )
    if not prep.ready:
        raise ApiError(
            ErrorCode.BLOCKED,
            "准备未完成或存在未清除差异/blockers，不能授权",
            details={"preparation_id": preparation_id, "blockers": prep.blockers},
        )
    if prep.prepared_digest != prepared_digest:
        raise ApiError(
            ErrorCode.CONFLICT_DIGEST,
            "prepared_digest 与准备产物摘要不一致；工程师确认的是真实产物摘要而非计划文字",
            details={"preparation_id": preparation_id},
        )
    if revision != prep.revision or revision != task.current_revision:
        raise ApiError(
            ErrorCode.CONFLICT_REVISION,
            "授权必须绑定当前修订与对应准备",
            details={"requested": revision, "current": task.current_revision},
        )

    consume_confirmation(
        session,
        identity,
        confirmation_id=confirmation_id,
        action="authorizeRuns",
        target_id=task_id,
        target_digest=prepared_digest,
    )

    transition_task_flow(task, TaskFlowState.AUTHORIZED)
    auth = AuthorizationRow(
        authorization_id=_new_id("authz"),
        task_id=task_id,
        revision=revision,
        preparation_id=preparation_id,
        prepared_digest=prepared_digest,
        execution_budget=execution_budget,
        authorized_by=identity.subject_id,
        purpose=task.purpose,
        validity="CURRENT",
    )
    session.add(auth)
    session.flush()
    return Authorization(
        authorization_id=auth.authorization_id,
        task_id=task_id,
        revision=revision,
        preparation_id=preparation_id,
        prepared_digest=prepared_digest,
        execution_budget=execution_budget,
        authorized_by=auth.authorized_by,
        purpose=auth.purpose,
        validity=auth.validity,
        created_at=auth.created_at,
        revoked_at=None,
    )


def submit_runs(
    session: Session,
    identity: Identity,
    task_id: str,
    *,
    authorization_id: str,
    prepared_digest: str,
) -> list[str]:
    """原子提交：授权校验 + Run 集合 + EXECUTE Job 一次事务（定义书 §三个重要原子操作 ①）。"""
    task = get_task_row(session, identity, task_id)
    auth = session.get(AuthorizationRow, authorization_id)
    if auth is None or auth.task_id != task_id:
        raise ApiError(
            ErrorCode.VALIDATION, "授权不存在或不属于该任务", details={"authorization_id": authorization_id}
        )
    if auth.validity != "CURRENT" or auth.revoked_at is not None:
        raise ApiError(
            ErrorCode.CONFLICT_REVISION,
            "授权已失效/已撤销（输入变化后旧授权不沿用）",
            details={"authorization_id": authorization_id},
        )
    if auth.prepared_digest != prepared_digest:
        raise ApiError(
            ErrorCode.CONFLICT_DIGEST,
            "准备摘要变化，409 拒绝（submitRuns 不重新猜测参数）",
            details={"authorization_id": authorization_id},
        )
    if auth.revision != task.current_revision:
        raise ApiError(
            ErrorCode.CONFLICT_REVISION,
            "授权绑定修订已非当前修订",
            details={"authorization_id": authorization_id},
        )

    rev = (
        session.query(TaskRevisionRow)
        .filter_by(task_id=task_id, revision=auth.revision)
        .first()
    )
    spec = rev.spec

    transition_task_flow(task, TaskFlowState.ACTIVE)

    run_ids: list[str] = []
    for variant in spec["variants"]:
        for condition in spec["conditions"]:
            assert_run_matrix_cell_free(
                session, task_id, auth.revision, variant["variant_id"], condition["condition_id"]
            )
            run = RunRow(
                run_id=_new_id("run"),
                task_id=task_id,
                revision=auth.revision,
                variant_id=variant["variant_id"],
                condition_id=condition["condition_id"],
                execution_state=ExecutionState.QUEUED.value,
                numerical_state=NumericalState.NOT_CHECKED.value,
                applicability_state=ApplicabilityState.UNCONFIRMED.value,
            )
            session.add(run)
            session.flush()
            attempt = AttemptRow(
                attempt_id=_new_id("att"),
                run_id=run.run_id,
                attempt_no=1,
                state=ExecutionState.QUEUED.value,
            )
            session.add(attempt)
            session.flush()
            run.current_attempt_id = attempt.attempt_id
            enqueue(
                session,
                kind="EXECUTE",
                task_id=task_id,
                run_id=run.run_id,
                attempt_id=attempt.attempt_id,
            )
            run_ids.append(run.run_id)
    session.flush()
    return run_ids


def get_run(
    session: Session, identity: Identity, run_id: str, *, after_seq: int = 0
) -> Run:
    run = session.get(RunRow, run_id)
    if run is None:
        raise ApiError(ErrorCode.VALIDATION, "Run 不存在", details={"run_id": run_id})
    get_task_row(session, identity, run.task_id)  # 项目权限
    job_ids = _job_ids_for_run(session, run_id)
    events = (
        session.query(EventRow)
        .filter(EventRow.job_id.in_(job_ids), EventRow.event_seq > after_seq)
        .order_by(EventRow.event_seq)
        .all()
        if job_ids
        else []
    )
    return Run(
        run_id=run.run_id,
        task_id=run.task_id,
        revision=run.revision,
        variant_id=run.variant_id,
        condition_id=run.condition_id,
        execution_state=ExecutionState(run.execution_state),
        numerical_state=NumericalState(run.numerical_state),
        applicability_state=ApplicabilityState(run.applicability_state),
        current_attempt_id=run.current_attempt_id,
        events=[
            Event(
                event_id=e.event_id,
                job_id=e.job_id,
                event_seq=e.event_seq,
                kind=e.kind,
                payload=e.payload,
                occurred_at=e.occurred_at,
            )
            for e in events
        ],
        created_at=run.created_at,
    )


def _job_ids_for_run(session: Session, run_id: str) -> list[str]:
    from dsh_sim.db.models import JobRow

    return [j.job_id for j in session.query(JobRow).filter_by(run_id=run_id).all()]


def cancel_run(session: Session, identity: Identity, run_id: str, *, reason: str | None) -> Run:
    """受控取消：请求入库，现场确认后才 CANCELLED（FR-12）。"""
    run = session.get(RunRow, run_id)
    if run is None:
        raise ApiError(ErrorCode.VALIDATION, "Run 不存在", details={"run_id": run_id})
    get_task_row(session, identity, run.task_id)
    job = find_active_job_for_run(session, run_id)
    if job is not None:
        request_cancel(session, job_id=job.job_id)
    session.flush()
    return get_run(session, identity, run_id)


def retry_run(
    session: Session,
    identity: Identity,
    run_id: str,
    *,
    reason: str,
    recovery_strategy: str,
) -> Attempt:
    """补算：同输入新增 attempt；受总预算限制；LOST 未核实冻结重派。"""
    if not reason or not recovery_strategy:
        raise ApiError(
            ErrorCode.VALIDATION, "补算必须给出 reason 与已批准 recovery_strategy"
        )
    run = session.get(RunRow, run_id)
    if run is None:
        raise ApiError(ErrorCode.VALIDATION, "Run 不存在", details={"run_id": run_id})
    get_task_row(session, identity, run.task_id)

    assert_single_open_attempt(session, run_id)

    rev = (
        session.query(TaskRevisionRow)
        .filter_by(task_id=run.task_id, revision=run.revision)
        .first()
    )
    max_attempts = int(
        (rev.spec.get("execution_budget") or {}).get("max_attempts_total", 1)
    )
    used_attempts = (
        session.query(AttemptRow)
        .join(RunRow, AttemptRow.run_id == RunRow.run_id)
        .filter(RunRow.task_id == run.task_id, RunRow.revision == run.revision)
        .count()
    )
    if used_attempts >= max_attempts:
        raise ApiError(
            ErrorCode.QUOTA,
            "总 attempt 预算已用尽（无无限重试；超预算阻塞）",
            details={"max_attempts_total": max_attempts, "used": used_attempts},
        )

    last_no = max(
        (a.attempt_no for a in session.query(AttemptRow).filter_by(run_id=run_id).all()),
        default=0,
    )
    attempt = AttemptRow(
        attempt_id=_new_id("att"),
        run_id=run_id,
        attempt_no=last_no + 1,
        state=ExecutionState.QUEUED.value,
    )
    session.add(attempt)
    session.flush()
    run.current_attempt_id = attempt.attempt_id
    # 新 attempt 使 Run 重新排队：Run 执行状态随当前 attempt 重置（Run 维度不是独立
    # 状态机，而是当前 attempt 的投影；旧 attempt 记录保留不覆盖 —— ADR-08）
    run.execution_state = ExecutionState.QUEUED.value
    enqueue(
        session, kind="EXECUTE", task_id=run.task_id, run_id=run_id, attempt_id=attempt.attempt_id
    )
    session.flush()
    return Attempt(
        attempt_id=attempt.attempt_id,
        run_id=run_id,
        attempt_no=attempt.attempt_no,
        node_id=None,
        lease_id=None,
        fencing_token=None,
        process_identity=None,
        state=ExecutionState.QUEUED,
        started_at=None,
        ended_at=None,
        created_at=attempt.created_at,
    )


__all__ = [
    "authorize_runs",
    "cancel_run",
    "consume_confirmation",
    "get_run",
    "issue_confirmation",
    "retry_run",
    "submit_runs",
]
