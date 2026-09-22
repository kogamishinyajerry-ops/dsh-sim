"""Pydantic v2 领域模型（与 contracts/openapi.v0.1.yaml components/schemas 对齐；
TaskSpec/TaskDraft 与 contracts/task-spec.schema.json / task-draft.schema.json 对齐）。

结构合法 ≠ 工程合法：引用存在性、项目权限、方法范围与跨字段一致性由服务层校验
（定义书 §输入契约与摘要规则）。
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from dsh_sim.domain.states import (
    ApplicabilityState,
    ExecutionState,
    NumericalState,
    ReviewState,
    TaskFlowState,
    ValidityState,
)


# ---------------------------------------------------------------------------
# 输入契约（task-spec / task-draft）
# ---------------------------------------------------------------------------


class Quantity(BaseModel):
    """工程数量：SI 规范值 + 单位 + 物理含义 + 来源一起保存。"""

    model_config = ConfigDict(extra="forbid")

    si_value: float
    unit: str = Field(min_length=1)
    physical_meaning: str = Field(min_length=1)
    source_ref: str = Field(min_length=1)
    original_value: float | None = None
    original_unit: str | None = None
    conversion_note: str | None = None


class PressureQuantity(Quantity):
    """压力：区分绝压/表压、总压/静压；表压必须携带参考绝压及来源（FR-04）。"""

    pressure_kind: Literal["absolute", "gauge"]
    pressure_semantics: Literal["total", "static"]
    reference_absolute_pressure: Quantity | None = None

    def model_post_init(self, __context: Any, /) -> None:
        if (
            self.pressure_kind == "gauge"
            and self.reference_absolute_pressure is None
        ):
            raise ValueError(
                "表压输入必须包含 reference_absolute_pressure（FR-04：没有参考压力的表压不得进入准备）"
            )


class Variant(BaseModel):
    """方案：引用已登记模板 artifact 与 boundary_map 摘要。数组顺序有意义。"""

    model_config = ConfigDict(extra="forbid")

    variant_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    template_artifact_id: str = Field(min_length=1)
    template_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    boundary_map_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ConditionField(BaseModel):
    """按稳定边界 role_id 填写的 field 与 quantity（白名单内）。"""

    model_config = ConfigDict(extra="forbid")

    role_id: str = Field(min_length=1)
    field: str = Field(min_length=1)
    quantity: Quantity | PressureQuantity


class Condition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    condition_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    fields: list[ConditionField] = Field(min_length=1)


class ExecutionBudgetSpec(BaseModel):
    """预算：并发 1—2；总 attempt 上限，无无限重试。"""

    model_config = ConfigDict(extra="forbid")

    max_concurrent: int = Field(ge=1, le=2)
    cpu_cores: int = Field(ge=1)
    memory_gb: float = Field(gt=0)
    wallclock_hours: float = Field(gt=0)
    max_attempts_total: int = Field(ge=1)


class Method(BaseModel):
    """方法：已发布能力包 ID + 摘要 + 必需指标 + 审查范围。"""

    model_config = ConfigDict(extra="forbid")

    capability_package_id: str = Field(min_length=1)
    capability_package_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    required_metrics: list[str] = Field(min_length=1)
    review_scope: str = Field(min_length=1)


class TaskSpec(BaseModel):
    """准备前的完整业务输入。TaskSpec 只承载业务输入；task_id、创建者与权限由服务产生。"""

    model_config = ConfigDict(extra="forbid")

    purpose: Literal["design_screening"] = "design_screening"
    variants: list[Variant] = Field(min_length=1, max_length=2)
    conditions: list[Condition] = Field(min_length=1, max_length=3)
    execution_budget: ExecutionBudgetSpec
    method: Method

    def model_post_init(self, __context: Any, /) -> None:
        # JSON Schema 无法表达跨元素唯一，由服务强制（schema 描述要求）
        variant_ids = [v.variant_id for v in self.variants]
        if len(variant_ids) != len(set(variant_ids)):
            raise ValueError("variant_id 在任务内必须唯一")
        condition_ids = [c.condition_id for c in self.conditions]
        if len(condition_ids) != len(set(condition_ids)):
            raise ValueError("condition_id 在任务内必须唯一")


class OpenQuestion(BaseModel):
    """阻塞数组一项：缺失/待澄清输入，含字段、责任人与问题（FR-01）。"""

    model_config = ConfigDict(extra="forbid")

    field: str = Field(min_length=1)
    responsible: str = Field(min_length=1)
    question: str = Field(min_length=1)


class TaskDraft(BaseModel):
    """任务草稿：允许缺整项，缺失必须 open_questions 显式阻塞，禁止补猜。"""

    model_config = ConfigDict(extra="forbid")

    purpose: Literal["design_screening"] = "design_screening"
    variants: list[Variant] | None = None
    conditions: list[Condition] | None = None
    execution_budget: ExecutionBudgetSpec | None = None
    method: Method | None = None
    open_questions: list[OpenQuestion] | None = None


# ---------------------------------------------------------------------------
# 资源模型（OpenAPI components/schemas）
# ---------------------------------------------------------------------------


class Task(BaseModel):
    task_id: str
    project_id: str
    current_revision: int = Field(ge=0)
    task_state: TaskFlowState
    review_state: ReviewState
    owner_id: str
    purpose: Literal["design_screening"] = "design_screening"
    blockers: list[dict[str, Any]] = Field(default_factory=list)
    created_at: datetime


class TaskRevision(BaseModel):
    """修订发布后不可原位修改（db/guards 强制）。"""

    task_id: str
    revision: int = Field(ge=1)
    spec: dict[str, Any]
    spec_sha256: str
    source_refs: list[str] = Field(default_factory=list)
    created_by: str
    created_at: datetime


class Preparation(BaseModel):
    """授权后不可变。"""

    preparation_id: str
    task_id: str
    revision: int = Field(ge=1)
    prepared_digest: str
    prepared_artifacts: dict[str, str] = Field(default_factory=dict)
    readback_sha256: str = ""
    adapter_build: str = ""
    software_build: str = ""
    differences: list[dict[str, Any]] = Field(default_factory=list)
    blockers: list[dict[str, Any]] = Field(default_factory=list)
    created_at: datetime


class Authorization(BaseModel):
    authorization_id: str
    task_id: str
    revision: int = Field(ge=1)
    preparation_id: str
    prepared_digest: str
    execution_budget: dict[str, Any]
    authorized_by: str
    purpose: str
    validity: ValidityState
    created_at: datetime
    revoked_at: datetime | None = None


class Event(BaseModel):
    event_id: str
    job_id: str
    event_seq: int = Field(ge=1)
    kind: Literal["STARTING", "RUNNING", "HEARTBEAT", "COMPLETED", "FAILED", "CANCELLED"]
    payload: dict[str, Any] = Field(default_factory=dict)
    occurred_at: datetime


class Run(BaseModel):
    run_id: str
    task_id: str
    revision: int = Field(ge=1)
    variant_id: str
    condition_id: str
    execution_state: ExecutionState
    numerical_state: NumericalState
    applicability_state: ApplicabilityState
    current_attempt_id: str | None = None
    events: list[Event] = Field(default_factory=list)
    created_at: datetime


class Attempt(BaseModel):
    """不可覆盖；补算产生新 attempt（ADR-08）。"""

    attempt_id: str
    run_id: str
    attempt_no: int = Field(ge=1)
    node_id: str | None = None
    lease_id: str | None = None
    fencing_token: int | None = None
    process_identity: dict[str, Any] | None = None
    state: ExecutionState
    started_at: datetime | None = None
    ended_at: datetime | None = None
    created_at: datetime


class JobKind(str, Enum):
    PREPARE = "PREPARE"  # 完成不生成工程结果，不进入已完成计算数
    EXECUTE = "EXECUTE"


class Job(BaseModel):
    job_id: str
    kind: JobKind
    task_id: str | None = None
    run_id: str | None = None
    attempt_id: str | None = None
    node_id: str | None = None
    state: ExecutionState
    row_version: int = Field(ge=0)
    created_at: datetime


class Lease(BaseModel):
    lease_id: str
    job_id: str
    node_id: str
    fencing_token: int = Field(ge=1)
    acquired_at: datetime
    expires_at: datetime


class ArtifactState(str, Enum):
    TEMP = "TEMP"
    COMMITTED = "COMMITTED"


class EvidenceMode(str, Enum):
    REAL = "REAL"
    MOCK = "MOCK"


class Artifact(BaseModel):
    """先临时上传，长度+摘要核验后原子提交。MOCK 绝不冒充 REAL（诚实红线）。"""

    artifact_id: str
    project_id: str
    logical_path: str
    length: int = Field(ge=0)
    sha256: str
    state: ArtifactState
    job_id: str | None = None
    run_id: str | None = None
    attempt_id: str | None = None
    evidence_mode: EvidenceMode | None = None
    created_at: datetime


class BundleManifestEntry(BaseModel):
    artifact_id: str
    logical_path: str
    sha256: str


class Bundle(BaseModel):
    """manifest 冻结，不可原位改写（db/guards 强制）。"""

    bundle_id: str
    task_id: str
    revision: int = Field(ge=1)
    bundle_digest: str
    manifest: list[BundleManifestEntry] = Field(default_factory=list)
    validity: ValidityState
    created_at: datetime


class Verification(BaseModel):
    verification_id: str
    run_id: str
    attempt_id: str | None = None
    rule_set_sha256: str
    check_inputs: dict[str, Any] = Field(default_factory=dict)
    conclusion: NumericalState
    findings: list[dict[str, Any]] = Field(default_factory=list)
    source_artifact_ids: list[str] = Field(default_factory=list)
    created_at: datetime


class Claim(BaseModel):
    """每条定量 Claim 绑定 metric_id 和 artifact_id；模型解释只是 DRAFT。"""

    claim_id: str
    bundle_id: str
    metric_id: str
    artifact_id: str
    text: str
    author_type: Literal["HUMAN", "AGENT"]
    state: Literal["DRAFT", "CONFIRMED"]
    created_at: datetime


class IssueReply(BaseModel):
    reply_id: str
    author_id: str
    body: str
    evidence_artifact_ids: list[str] = Field(default_factory=list)
    created_at: datetime


class IssueStatus(str, Enum):
    DRAFT = "DRAFT"
    OPEN = "OPEN"
    CLOSED = "CLOSED"


class ReviewIssue(BaseModel):
    """Agent 只能起草或回复，不能把问题设为 CLOSED（定义书 §整改项字段）。"""

    issue_id: str
    review_id: str
    created_by: str
    responsible: str
    severity: str
    description: str
    close_criteria: str
    related_artifact_ids: list[str] = Field(default_factory=list)
    related_run_ids: list[str] = Field(default_factory=list)
    replies: list[IssueReply] = Field(default_factory=list)
    status: IssueStatus
    version: int = Field(ge=1)
    closed_by: str | None = None
    closed_at: datetime | None = None
    created_at: datetime


class Review(BaseModel):
    review_id: str
    task_id: str
    revision: int = Field(ge=1)
    bundle_id: str
    bundle_digest: str
    reviewer_id: str
    state: ReviewState
    validity: ValidityState
    issues: list[ReviewIssue] = Field(default_factory=list)
    created_at: datetime


class DecisionOutcome(str, Enum):
    ACCEPT = "ACCEPT"
    REQUEST_CHANGES = "REQUEST_CHANGES"
    REJECT = "REJECT"


class Decision(BaseModel):
    """不可变。ACCEPT 在数值 FAIL/证据 INSUFFICIENT/范围 UNCONFIRMED/MOCK/未关闭阻塞时必须失败。"""

    decision_id: str
    review_id: str
    outcome: DecisionOutcome
    decided_by: str
    task_id: str
    revision: int = Field(ge=1)
    bundle_digest: str
    purpose: str
    limitations: str | None = None
    decided_at: datetime


class CapabilityStatus(str, Enum):
    DRAFT = "DRAFT"
    RELEASED = "RELEASED"
    RETIRED = "RETIRED"


class CapabilityPackage(BaseModel):
    """只有 RELEASED 且与当前软件匹配的能力可用于正式任务。"""

    capability_package_id: str
    version: str
    manifest_sha256: str
    status: CapabilityStatus
    purpose: str
    domain_summary: str | None = None
    compatibility: dict[str, Any] | None = None


class Error(BaseModel):
    """统一错误对象（CONVENTIONS §3.3）。"""

    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    retryable: bool
    trace_id: str
    details: dict[str, Any] = Field(default_factory=dict)


__all__ = [
    "Artifact",
    "ArtifactState",
    "Attempt",
    "Authorization",
    "Bundle",
    "BundleManifestEntry",
    "CapabilityPackage",
    "CapabilityStatus",
    "Claim",
    "Condition",
    "ConditionField",
    "Decision",
    "DecisionOutcome",
    "Error",
    "Event",
    "EvidenceMode",
    "ExecutionBudgetSpec",
    "IssueReply",
    "IssueStatus",
    "Job",
    "JobKind",
    "Lease",
    "Method",
    "OpenQuestion",
    "Preparation",
    "PressureQuantity",
    "Quantity",
    "Review",
    "ReviewIssue",
    "Run",
    "Task",
    "TaskDraft",
    "TaskRevision",
    "TaskSpec",
    "Variant",
    "Verification",
]
