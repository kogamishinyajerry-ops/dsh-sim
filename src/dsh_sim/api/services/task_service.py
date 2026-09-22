"""任务服务：createTask / createRevision（定义书 §工程API：业务操作）。

- createTask：草稿（task-draft.schema.json），缺项以 blockers/open_questions 显式表达。
- createRevision：完整 TaskSpec 校验 + expected_revision 乐观锁；新修订使旧授权/包/
  审查失效（STALE），历史保留（FR-23）。spec_sha256 由服务侧经 canonical-json-v1 计算。
"""
from __future__ import annotations

import uuid
from typing import Any

from pydantic import ValidationError
from sqlalchemy import update
from sqlalchemy.orm import Session

from dsh_sim.canonical import spec_sha256
from dsh_sim.db.models import (
    AuthorizationRow,
    BundleRow,
    ReviewRow,
    TaskRevisionRow,
    TaskRow,
    utcnow,
)
from dsh_sim.domain.errors import ApiError, ErrorCode
from dsh_sim.domain.identity import Identity
from dsh_sim.domain.schemas import Task, TaskDraft, TaskRevision, TaskSpec
from dsh_sim.domain.states import TaskFlowState, validate_transition


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:24]}"


def get_task_row(session: Session, identity: Identity, task_id: str) -> TaskRow:
    task = session.get(TaskRow, task_id)
    if task is None:
        raise ApiError(ErrorCode.VALIDATION, "任务不存在", details={"task_id": task_id})
    if not identity.can_access_project(task.project_id):
        raise ApiError(
            ErrorCode.FORBIDDEN,
            "跨项目读取被拒绝（FR-24 项目级数据隔离）",
            details={"task_id": task_id},
        )
    return task


def create_task(session: Session, identity: Identity, project_id: str, draft: dict[str, Any]) -> Task:
    try:
        parsed = TaskDraft.model_validate(draft)
    except ValidationError as exc:
        raise ApiError(
            ErrorCode.VALIDATION,
            "任务草稿不符合 task-draft.schema.json",
            details={"errors": exc.errors(include_url=False)},
        ) from exc
    if project_id == "-" or not identity.can_access_project(project_id):
        raise ApiError(ErrorCode.FORBIDDEN, "身份不含可用项目，无法创建任务")

    task_id = _new_id("task")
    blockers = [q.model_dump() for q in (parsed.open_questions or [])]
    row = TaskRow(
        task_id=task_id,
        project_id=project_id,
        owner_id=identity.subject_id,
        purpose=parsed.purpose,
        current_revision=0,
        task_state=TaskFlowState.DRAFT.value,
        blockers=blockers,
    )
    session.add(row)
    session.flush()
    return Task(
        task_id=row.task_id,
        project_id=row.project_id,
        current_revision=row.current_revision,
        task_state=TaskFlowState(row.task_state),
        review_state=row.review_state,
        owner_id=row.owner_id,
        purpose=row.purpose,
        blockers=row.blockers,
        created_at=row.created_at,
    )


def create_revision(
    session: Session,
    identity: Identity,
    task_id: str,
    *,
    expected_revision: int,
    spec: dict[str, Any],
    source_refs: list[str] | None = None,
) -> TaskRevision:
    task = get_task_row(session, identity, task_id)

    # 完整 TaskSpec 校验（准备前剔除草稿字段；结构合法 ≠ 工程合法，范围检查在准备时进行）
    try:
        parsed = TaskSpec.model_validate(spec)
    except ValidationError as exc:
        raise ApiError(
            ErrorCode.VALIDATION,
            "修订输入不符合 task-spec.schema.json",
            details={"errors": exc.errors(include_url=False)},
        ) from exc

    if expected_revision != task.current_revision:
        raise ApiError(
            ErrorCode.CONFLICT_REVISION,
            "expected_revision 与当前修订不一致（乐观锁防并发覆盖）",
            details={"expected": expected_revision, "current": task.current_revision},
        )

    spec_dict = parsed.model_dump(mode="json", exclude_none=True)
    digest = spec_sha256(spec_dict)  # 哈希仅由服务计算（canonical-json-v1）
    new_revision = task.current_revision + 1

    rev_row = TaskRevisionRow(
        task_id=task_id,
        revision=new_revision,
        spec=spec_dict,
        spec_sha256=digest,
        source_refs=source_refs or [],
        created_by=identity.subject_id,
    )
    session.add(rev_row)

    # current_revision 只能以乐观锁更新（定义书 §数据库约束）：
    # PG 下同样以 WHERE row_version 防并发；SQLite 由 BEGIN IMMEDIATE 串行化。
    result = session.execute(
        update(TaskRow)
        .where(TaskRow.task_id == task_id, TaskRow.row_version == task.row_version)
        .values(
            current_revision=new_revision,
            task_state=TaskFlowState.DRAFT.value,  # 新修订重新走流程
            review_state="NOT_SUBMITTED",
            blockers=[],
            row_version=TaskRow.row_version + 1,
        )
    )
    if result.rowcount != 1:
        raise ApiError(
            ErrorCode.CONFLICT_REVISION,
            "任务被并发修改，乐观锁失败",
            details={"task_id": task_id},
        )

    # 新修订使旧授权/旧包/旧审查失效（STALE），历史记录保留不删除（FR-23）
    session.execute(
        update(AuthorizationRow)
        .where(AuthorizationRow.task_id == task_id, AuthorizationRow.validity == "CURRENT")
        .values(validity="STALE")
    )
    session.execute(
        update(BundleRow)
        .where(BundleRow.task_id == task_id, BundleRow.validity == "CURRENT")
        .values(validity="STALE")
    )
    session.execute(
        update(ReviewRow)
        .where(ReviewRow.task_id == task_id, ReviewRow.validity == "CURRENT")
        .values(validity="STALE")
    )
    session.flush()

    return TaskRevision(
        task_id=task_id,
        revision=new_revision,
        spec=spec_dict,
        spec_sha256=digest,
        source_refs=rev_row.source_refs,
        created_by=identity.subject_id,
        created_at=rev_row.created_at,
    )


def transition_task_flow(task: TaskRow, target: TaskFlowState) -> None:
    """任务流程迁移入口：非法迁移直接抛错（服务端 bug 或并发破坏，不外泄为 2xx）。"""
    validate_transition(TaskFlowState(task.task_state), target)
    task.task_state = target.value
    task.row_version += 1


__all__ = ["create_revision", "create_task", "get_task_row", "transition_task_flow"]
