"""五维状态枚举与迁移规则（定义书 §任务、数值、适用与审查状态；CONVENTIONS §3.2）。

值与定义书一字不差。每个枚举附 allowed_transitions 映射与 validate_transition 纯函数；
非法迁移抛 InvalidTransitionError。

关键语义（定义书原文，禁止推断）：
- READY 仅表示准备完成且回读一致，不表示工程结果已通过。
- SUCCEEDED 仅证明执行与规定文件收集成功；程序成功不得自动写 PASS。
- 数值 PASS 不证明物理方法适用。
- 模型推荐通过不等于人工 ACCEPTED。
- LOST 表示外部程序状态未知，冻结重派待核实（定义书 §作业生命周期）。
"""
from __future__ import annotations

from enum import Enum


class InvalidTransitionError(ValueError):
    """非法状态迁移。"""

    def __init__(self, dimension: str, from_state: Enum, to_state: Enum) -> None:
        super().__init__(
            f"{dimension}: 非法迁移 {from_state.value} -> {to_state.value}"
        )
        self.dimension = dimension
        self.from_state = from_state
        self.to_state = to_state


class TaskFlowState(str, Enum):
    """任务流程。终态 ACCEPTED / CHANGES_REQUESTED / REJECTED。"""

    DRAFT = "DRAFT"
    PREPARING = "PREPARING"
    READY = "READY"
    AUTHORIZED = "AUTHORIZED"
    ACTIVE = "ACTIVE"
    READY_FOR_REVIEW = "READY_FOR_REVIEW"
    IN_REVIEW = "IN_REVIEW"
    ACCEPTED = "ACCEPTED"
    CHANGES_REQUESTED = "CHANGES_REQUESTED"
    REJECTED = "REJECTED"


class ExecutionState(str, Enum):
    """执行状态（Job/Attempt/Run 共用）。"""

    QUEUED = "QUEUED"
    WAITING_RESOURCE = "WAITING_RESOURCE"
    LEASED = "LEASED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    CANCELLING = "CANCELLING"
    COLLECTING = "COLLECTING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    LOST = "LOST"


class NumericalState(str, Enum):
    """数值状态。程序成功 ≠ PASS（ADR-09）。"""

    NOT_CHECKED = "NOT_CHECKED"
    PASS = "PASS"
    FAIL = "FAIL"
    INSUFFICIENT = "INSUFFICIENT"


class ApplicabilityState(str, Enum):
    """适用性状态。范围未知统一 UNCONFIRMED（TBD-04）。"""

    IN_SCOPE = "IN_SCOPE"
    OUT_OF_SCOPE = "OUT_OF_SCOPE"
    UNCONFIRMED = "UNCONFIRMED"


class ReviewState(str, Enum):
    """审查状态。"""

    NOT_SUBMITTED = "NOT_SUBMITTED"
    PENDING = "PENDING"
    CHANGES_REQUESTED = "CHANGES_REQUESTED"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"


class ValidityState(str, Enum):
    """有效性。旧版批准不沿用到新版；历史决定不删除。"""

    CURRENT = "CURRENT"
    STALE = "STALE"


# ---------------------------------------------------------------------------
# 迁移矩阵
# ---------------------------------------------------------------------------

TASK_FLOW_TRANSITIONS: dict[TaskFlowState, frozenset[TaskFlowState]] = {
    TaskFlowState.DRAFT: frozenset({TaskFlowState.PREPARING}),
    # 准备失败带 blockers 停在可解释阶段，允许回到 DRAFT 修订输入（不静默跳过）
    TaskFlowState.PREPARING: frozenset({TaskFlowState.READY, TaskFlowState.DRAFT}),
    TaskFlowState.READY: frozenset({TaskFlowState.AUTHORIZED, TaskFlowState.PREPARING}),
    TaskFlowState.AUTHORIZED: frozenset({TaskFlowState.ACTIVE}),
    TaskFlowState.ACTIVE: frozenset({TaskFlowState.READY_FOR_REVIEW}),
    TaskFlowState.READY_FOR_REVIEW: frozenset({TaskFlowState.IN_REVIEW}),
    TaskFlowState.IN_REVIEW: frozenset(
        {
            TaskFlowState.ACCEPTED,
            TaskFlowState.CHANGES_REQUESTED,
            TaskFlowState.REJECTED,
        }
    ),
    # 终态：无出边。退回整改通过新修订重新走流程（定义书 §修订、整改与历史批准）。
    TaskFlowState.ACCEPTED: frozenset(),
    TaskFlowState.CHANGES_REQUESTED: frozenset(),
    TaskFlowState.REJECTED: frozenset(),
}

EXECUTION_TRANSITIONS: dict[ExecutionState, frozenset[ExecutionState]] = {
    ExecutionState.QUEUED: frozenset(
        {
            ExecutionState.WAITING_RESOURCE,
            ExecutionState.LEASED,
            # 未出租作业无外部进程，取消请求入库即可现场确认 CANCELLED
            ExecutionState.CANCELLED,
        }
    ),
    ExecutionState.WAITING_RESOURCE: frozenset(
        {
            ExecutionState.QUEUED,
            ExecutionState.LEASED,
            ExecutionState.CANCELLED,
        }
    ),
    ExecutionState.LEASED: frozenset(
        {
            ExecutionState.STARTING,
            ExecutionState.CANCELLING,
            # Worker 报 CANCELLED（含退出证明）即现场确认，可直接落定；
            # 异步语义由 request_cancel 的 cancel_requested + CANCELLING 保证
            ExecutionState.CANCELLED,
            ExecutionState.LOST,  # 租约到期失联 → LOST 待核实，不自动重派
        }
    ),
    ExecutionState.STARTING: frozenset(
        {
            ExecutionState.RUNNING,
            ExecutionState.FAILED,
            ExecutionState.CANCELLING,
            ExecutionState.CANCELLED,
            ExecutionState.LOST,
        }
    ),
    ExecutionState.RUNNING: frozenset(
        {
            ExecutionState.COLLECTING,
            ExecutionState.FAILED,
            ExecutionState.CANCELLING,
            ExecutionState.CANCELLED,
            ExecutionState.LOST,
        }
    ),
    ExecutionState.CANCELLING: frozenset(
        {
            ExecutionState.CANCELLED,  # 现场确认全部受控子进程退出后才记 CANCELLED
            ExecutionState.LOST,  # 无法证实退出 → LOST，不释放为可重跑（FR-12）
            ExecutionState.FAILED,
        }
    ),
    ExecutionState.COLLECTING: frozenset(
        {
            ExecutionState.SUCCEEDED,
            ExecutionState.FAILED,
            ExecutionState.LOST,
        }
    ),
    # 终态。LOST 允许人工核实后裁定 FAILED（只此一出口，且必须留痕）；
    # SUCCEEDED/FAILED/CANCELLED 无出边，补算走新 attempt（ADR-08）。
    ExecutionState.SUCCEEDED: frozenset(),
    ExecutionState.FAILED: frozenset(),
    ExecutionState.CANCELLED: frozenset(),
    ExecutionState.LOST: frozenset({ExecutionState.FAILED}),
}

NUMERICAL_TRANSITIONS: dict[NumericalState, frozenset[NumericalState]] = {
    NumericalState.NOT_CHECKED: frozenset(
        {NumericalState.PASS, NumericalState.FAIL, NumericalState.INSUFFICIENT}
    ),
    # 重新检查产生新 Verification 记录，不原位改状态（定义书 §修订：旧数值不原位改写）
    NumericalState.PASS: frozenset(),
    NumericalState.FAIL: frozenset(),
    NumericalState.INSUFFICIENT: frozenset(),
}

APPLICABILITY_TRANSITIONS: dict[ApplicabilityState, frozenset[ApplicabilityState]] = {
    ApplicabilityState.UNCONFIRMED: frozenset(
        {ApplicabilityState.IN_SCOPE, ApplicabilityState.OUT_OF_SCOPE}
    ),
    # 方法包撤回/证据范围变化 → 回到 UNCONFIRMED 待复核（定义书 §修订：标记待复核）
    ApplicabilityState.IN_SCOPE: frozenset(
        {ApplicabilityState.UNCONFIRMED, ApplicabilityState.OUT_OF_SCOPE}
    ),
    ApplicabilityState.OUT_OF_SCOPE: frozenset({ApplicabilityState.UNCONFIRMED}),
}

REVIEW_TRANSITIONS: dict[ReviewState, frozenset[ReviewState]] = {
    ReviewState.NOT_SUBMITTED: frozenset({ReviewState.PENDING}),
    ReviewState.PENDING: frozenset(
        {
            ReviewState.CHANGES_REQUESTED,
            ReviewState.ACCEPTED,
            ReviewState.REJECTED,
        }
    ),
    # 退回补充后可再次提交新冻结包（产生新 Review 记录时为 NOT_SUBMITTED→PENDING；
    # 同一 Review 上允许 CHANGES_REQUESTED→PENDING 表示补充后再审）
    ReviewState.CHANGES_REQUESTED: frozenset({ReviewState.PENDING}),
    ReviewState.ACCEPTED: frozenset(),  # 已接受决定不可变（定义书 §接受后的历史）
    ReviewState.REJECTED: frozenset(),
}

VALIDITY_TRANSITIONS: dict[ValidityState, frozenset[ValidityState]] = {
    ValidityState.CURRENT: frozenset({ValidityState.STALE}),
    ValidityState.STALE: frozenset(),  # 失效不可逆；历史保留
}

_TRANSITION_MAPS: dict[type[Enum], dict[Enum, frozenset[Enum]]] = {
    TaskFlowState: TASK_FLOW_TRANSITIONS,
    ExecutionState: EXECUTION_TRANSITIONS,
    NumericalState: NUMERICAL_TRANSITIONS,
    ApplicabilityState: APPLICABILITY_TRANSITIONS,
    ReviewState: REVIEW_TRANSITIONS,
    ValidityState: VALIDITY_TRANSITIONS,
}


def allowed_transitions(state: Enum) -> frozenset[Enum]:
    """返回某状态的全部合法后继。"""
    return _TRANSITION_MAPS[type(state)][state]


def is_terminal(state: Enum) -> bool:
    return len(_TRANSITION_MAPS[type(state)][state]) == 0


def validate_transition(from_state: Enum, to_state: Enum) -> None:
    """校验迁移合法性；非法迁移抛 InvalidTransitionError。纯函数，无副作用。"""
    if type(from_state) is not type(to_state):
        raise InvalidTransitionError("cross-dimension", from_state, to_state)
    if to_state not in _TRANSITION_MAPS[type(from_state)][from_state]:
        raise InvalidTransitionError(
            type(from_state).__name__, from_state, to_state
        )


def can_transition(from_state: Enum, to_state: Enum) -> bool:
    """布尔版迁移校验，供守卫与测试使用。"""
    return (
        type(from_state) is type(to_state)
        and to_state in _TRANSITION_MAPS[type(from_state)][from_state]
    )


# 执行维度的终态集合（不含 LOST：LOST 是"未知"，有唯一人工裁定出口 FAILED）
EXECUTION_TERMINAL: frozenset[ExecutionState] = frozenset(
    {ExecutionState.SUCCEEDED, ExecutionState.FAILED, ExecutionState.CANCELLED}
)

# 未核实结束的执行状态集合（含 LOST）：每 Run 最多一个处于这些状态的真实 attempt
EXECUTION_OPEN: frozenset[ExecutionState] = frozenset(
    s for s in ExecutionState if s not in EXECUTION_TERMINAL
)

__all__ = [
    "APPLICABILITY_TRANSITIONS",
    "ApplicabilityState",
    "EXECUTION_OPEN",
    "EXECUTION_TERMINAL",
    "EXECUTION_TRANSITIONS",
    "ExecutionState",
    "InvalidTransitionError",
    "NUMERICAL_TRANSITIONS",
    "NumericalState",
    "REVIEW_TRANSITIONS",
    "ReviewState",
    "TASK_FLOW_TRANSITIONS",
    "TaskFlowState",
    "VALIDITY_TRANSITIONS",
    "ValidityState",
    "allowed_transitions",
    "can_transition",
    "is_terminal",
    "validate_transition",
]
