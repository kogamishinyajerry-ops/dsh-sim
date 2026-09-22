"""不可变性显式守卫（定义书 §数据库约束；WP-04）。

models.py 尾部的 before_update 事件是最后一道防线；本模块提供业务层应显式调用的
守卫函数，让"不允许的操作"在服务代码里可读、可测，而不是依赖隐式行为。
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from dsh_sim.db.models import AttemptRow, BundleRow, RunRow
from dsh_sim.domain.errors import ApiError, ErrorCode
from dsh_sim.domain.states import EXECUTION_OPEN


def guard_revision_immutable() -> None:
    """修订输入不可更新、仅新增：本函数即规则文档。任何试图 UPDATE task_revisions
    的代码路径都会被 models.py 的 before_update 事件拒绝；业务层不应提供修订更新接口。"""
    raise ApiError(
        ErrorCode.CONFLICT_REVISION,
        "TaskRevision 发布后不可原位修改；请创建新修订（revision 单调递增）",
    )


def guard_bundle_manifest_immutable(bundle: BundleRow) -> None:
    """Bundle 清单不可原位改写：改动 manifest 必须构建新 Bundle（新 bundle_id）。"""
    raise ApiError(
        ErrorCode.CONFLICT_DIGEST,
        "Bundle manifest 冻结不可改写；内容变化必须 buildBundle 产生新包",
        details={"bundle_id": bundle.bundle_id},
    )


def assert_single_open_attempt(session: Session, run_id: str) -> None:
    """每 Run 最多一个未核实结束的真实 attempt（定义书 §数据库约束）。

    未核实结束 = 执行状态属于 EXECUTION_OPEN（含 LOST）。LOST 冻结重派：
    未证实旧 attempt 已退出前，不得创建新求解器（定义书 §作业生命周期）。
    """
    open_attempts = (
        session.query(AttemptRow)
        .filter(AttemptRow.run_id == run_id, AttemptRow.state.in_(sorted(EXECUTION_OPEN)))
        .all()
    )
    if open_attempts:
        raise ApiError(
            ErrorCode.CONFLICT_REVISION,
            "该 Run 存在未核实结束的 attempt（含 LOST 待核实），冻结重派；"
            "须先核实旧 attempt 现场后再补算",
            details={
                "run_id": run_id,
                "open_attempt_ids": [a.attempt_id for a in open_attempts],
            },
        )


def assert_run_matrix_cell_free(
    session: Session, task_id: str, revision: int, variant_id: str, condition_id: str
) -> None:
    """UNIQUE(task_id,revision,variant_id,condition_id) 的应用层前置检查，
    给出 409 语义而不是裸 IntegrityError。"""
    existing = (
        session.query(RunRow)
        .filter_by(
            task_id=task_id, revision=revision, variant_id=variant_id, condition_id=condition_id
        )
        .first()
    )
    if existing is not None:
        raise ApiError(
            ErrorCode.CONFLICT_REVISION,
            "Run 矩阵单元已存在（task_id,revision,variant_id,condition_id 唯一）",
            details={"run_id": existing.run_id},
        )


__all__ = [
    "assert_run_matrix_cell_free",
    "assert_single_open_attempt",
    "guard_bundle_manifest_immutable",
    "guard_revision_immutable",
]
