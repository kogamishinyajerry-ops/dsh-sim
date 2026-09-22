"""准备服务：prepareTask / getPreparation（定义书 §准备与授权）。

prepareTask 只创建异步 PREPARE 作业并迁移 DRAFT→PREPARING；不在该动作暗中开始正式求解。
真实 STAR 准备链（prepare_case/read_actual_settings）由 Worker + adapters 完成（Agent E），
本层在 Preparation.blockers 中显式标记等待执行链，绝不补造回读数据（诚实红线）。
"""
from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from dsh_sim.canonical import prepared_digest as compute_prepared_digest
from dsh_sim.canonical import sha256_hex
from dsh_sim.db.models import PreparationRow, TaskRevisionRow, utcnow
from dsh_sim.domain.errors import ApiError, ErrorCode
from dsh_sim.domain.identity import Identity
from dsh_sim.domain.schemas import Preparation
from dsh_sim.domain.states import TaskFlowState
from dsh_sim.queue.service import enqueue

from dsh_sim.api.services.task_service import get_task_row, transition_task_flow


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:24]}"


def prepare_task(
    session: Session, identity: Identity, task_id: str, *, revision: int
) -> tuple[str, str]:
    """创建异步准备作业。返回 (preparation_id, job_id)。"""
    task = get_task_row(session, identity, task_id)
    if revision != task.current_revision or revision < 1:
        raise ApiError(
            ErrorCode.CONFLICT_REVISION,
            "只能对当前修订发起准备",
            details={"requested": revision, "current": task.current_revision},
        )
    rev = (
        session.query(TaskRevisionRow)
        .filter_by(task_id=task_id, revision=revision)
        .first()
    )
    if rev is None:
        raise ApiError(ErrorCode.VALIDATION, "修订不存在", details={"revision": revision})

    # DRAFT → PREPARING（READY 下重新准备：READY → PREPARING 也是合法迁移）
    transition_task_flow(task, TaskFlowState.PREPARING)

    preparation_id = _new_id("prep")
    # 准备未完成时 digest 未知：占位值由 preparation_id 派生（唯一），完成后由执行链
    # 以 compute_prepared_digest 真实重算（mark_preparation_ready）。
    placeholder_digest = sha256_hex(f"pending:{preparation_id}")
    prep = PreparationRow(
        preparation_id=preparation_id,
        task_id=task_id,
        revision=revision,
        prepared_digest=placeholder_digest,
        prepared_artifacts={},
        readback_sha256="",
        adapter_build="",
        software_build="",
        differences=[],
        blockers=[
            {
                "kind": "BLOCKED",
                "detail": "等待 PREPARE 作业被 Worker 执行（STAR 适配器执行链为 Agent E 范围）",
                "job_kind": "PREPARE",
            }
        ],
        ready=False,
    )
    session.add(prep)
    job = enqueue(session, kind="PREPARE", task_id=task_id)
    session.flush()
    return preparation_id, job.job_id


def get_preparation(session: Session, identity: Identity, preparation_id: str) -> Preparation:
    prep = session.get(PreparationRow, preparation_id)
    if prep is None:
        raise ApiError(
            ErrorCode.VALIDATION, "准备不存在", details={"preparation_id": preparation_id}
        )
    get_task_row(session, identity, prep.task_id)  # 项目权限检查
    return Preparation(
        preparation_id=prep.preparation_id,
        task_id=prep.task_id,
        revision=prep.revision,
        prepared_digest=prep.prepared_digest,
        prepared_artifacts=prep.prepared_artifacts,
        readback_sha256=prep.readback_sha256,
        adapter_build=prep.adapter_build,
        software_build=prep.software_build,
        differences=prep.differences,
        blockers=prep.blockers,
        created_at=prep.created_at,
    )


def mark_preparation_ready(
    session: Session,
    preparation_id: str,
    *,
    prepared_artifacts: dict[str, str],
    readback_sha256: str,
    adapter_build: str,
    software_build: str,
    differences: list[dict] | None = None,
) -> PreparationRow:
    """执行链（Agent E Worker）完成准备后的落库入口。

    prepared_digest = f(spec_sha, 准备产物摘要, 真实回读摘要, 适配器构建)（canonical §3.1）。
    所有必需项一致才进入 READY；存在差异/缺项时由调用方把 blockers 留在记录上并停住。
    """
    prep = session.get(PreparationRow, preparation_id)
    if prep is None:
        raise ApiError(
            ErrorCode.VALIDATION, "准备不存在", details={"preparation_id": preparation_id}
        )
    rev = (
        session.query(TaskRevisionRow)
        .filter_by(task_id=prep.task_id, revision=prep.revision)
        .first()
    )
    digest = compute_prepared_digest(
        rev.spec_sha256, prepared_artifacts, readback_sha256, adapter_build
    )
    prep.prepared_digest = digest
    prep.prepared_artifacts = prepared_artifacts
    prep.readback_sha256 = readback_sha256
    prep.adapter_build = adapter_build
    prep.software_build = software_build
    prep.differences = differences or []
    if not prep.differences:
        prep.blockers = []
        prep.ready = True
        # READY 仅表示准备完成且回读一致，不表示工程结果已通过
        from dsh_sim.db.models import TaskRow

        task_row = session.get(TaskRow, prep.task_id)
        transition_task_flow(task_row, TaskFlowState.READY)
    session.flush()
    return prep


__all__ = ["get_preparation", "mark_preparation_ready", "prepare_task"]
