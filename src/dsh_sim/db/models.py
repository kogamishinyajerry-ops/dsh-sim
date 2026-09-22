"""SQLAlchemy 2.x 声明式模型（定义书 §数据库约束与事务边界；WP-04）。

落实的硬约束：
- tasks / task_revisions：UNIQUE(task_id, revision)；current_revision 乐观锁 row_version；
  修订输入不可更新（应用层守卫 db/guards.py + before_update 事件，见文件尾部）。
- preparations / authorizations：UNIQUE(task_id, revision, prepared_digest)；
  授权绑定授权人/用途/预算/prepared_digest；撤销留痕 revoked_at。
- runs / attempts：UNIQUE(task_id,revision,variant_id,condition_id)；UNIQUE(run_id,attempt_no)。
- jobs / leases / events：Job 状态+row_version；leases fencing_token 递增（job.fencing_counter）；
  UNIQUE(job_id,event_seq)。
- artifacts / bundles：artifact TEMP→COMMITTED；bundle manifest 不可原位改写（应用层守卫）。
- reviews / issues / decisions / idempotency_records：按定义书列约束。
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from dsh_sim.domain.errors import ApiError, ErrorCode


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# 任务与修订
# ---------------------------------------------------------------------------


class TaskRow(Base):
    __tablename__ = "tasks"

    task_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(64), index=True)
    owner_id: Mapped[str] = mapped_column(String(128))
    purpose: Mapped[str] = mapped_column(String(32), default="design_screening")
    current_revision: Mapped[int] = mapped_column(Integer, default=0)
    task_state: Mapped[str] = mapped_column(String(32), default="DRAFT")
    review_state: Mapped[str] = mapped_column(String(32), default="NOT_SUBMITTED")
    blockers: Mapped[list] = mapped_column(JSON, default=list)
    # 乐观锁：current_revision 只能以乐观锁更新（定义书 §数据库约束）
    row_version: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TaskRevisionRow(Base):
    __tablename__ = "task_revisions"
    __table_args__ = (UniqueConstraint("task_id", "revision", name="uq_task_revision"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.task_id"), index=True)
    revision: Mapped[int] = mapped_column(Integer)
    spec: Mapped[dict] = mapped_column(JSON)
    spec_sha256: Mapped[str] = mapped_column(String(64))
    source_refs: Mapped[list] = mapped_column(JSON, default=list)
    created_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    # 不可变性：修订发布后不可原位修改，仅新增（定义书 §数据对象与关系）。
    # 强制见文件尾部 before_update 事件 + db/guards.py。


# ---------------------------------------------------------------------------
# 准备与授权
# ---------------------------------------------------------------------------


class PreparationRow(Base):
    __tablename__ = "preparations"
    __table_args__ = (
        # 准备绑定唯一修订及 digest（定义书 §数据库约束）
        UniqueConstraint("task_id", "revision", "prepared_digest", name="uq_prep_rev_digest"),
    )

    preparation_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.task_id"), index=True)
    revision: Mapped[int] = mapped_column(Integer)
    prepared_digest: Mapped[str] = mapped_column(String(64))
    prepared_artifacts: Mapped[dict] = mapped_column(JSON, default=dict)
    readback_sha256: Mapped[str] = mapped_column(String(64), default="")
    adapter_build: Mapped[str] = mapped_column(String(256), default="")
    software_build: Mapped[str] = mapped_column(String(256), default="")
    differences: Mapped[list] = mapped_column(JSON, default=list)
    blockers: Mapped[list] = mapped_column(JSON, default=list)
    ready: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuthorizationRow(Base):
    __tablename__ = "authorizations"

    authorization_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.task_id"), index=True)
    revision: Mapped[int] = mapped_column(Integer)
    preparation_id: Mapped[str] = mapped_column(ForeignKey("preparations.preparation_id"))
    prepared_digest: Mapped[str] = mapped_column(String(64))
    execution_budget: Mapped[dict] = mapped_column(JSON)
    authorized_by: Mapped[str] = mapped_column(String(128))
    purpose: Mapped[str] = mapped_column(String(64), default="design_screening")
    validity: Mapped[str] = mapped_column(String(16), default="CURRENT")
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ---------------------------------------------------------------------------
# Run / Attempt / Job / Lease / Event
# ---------------------------------------------------------------------------


class RunRow(Base):
    __tablename__ = "runs"
    __table_args__ = (
        UniqueConstraint(
            "task_id", "revision", "variant_id", "condition_id", name="uq_run_matrix_cell"
        ),
    )

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.task_id"), index=True)
    revision: Mapped[int] = mapped_column(Integer)
    variant_id: Mapped[str] = mapped_column(String(64))
    condition_id: Mapped[str] = mapped_column(String(64))
    execution_state: Mapped[str] = mapped_column(String(32), default="QUEUED")
    numerical_state: Mapped[str] = mapped_column(String(32), default="NOT_CHECKED")
    applicability_state: Mapped[str] = mapped_column(String(32), default="UNCONFIRMED")
    current_attempt_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AttemptRow(Base):
    __tablename__ = "attempts"
    __table_args__ = (UniqueConstraint("run_id", "attempt_no", name="uq_run_attempt_no"),)

    attempt_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.run_id"), index=True)
    attempt_no: Mapped[int] = mapped_column(Integer)
    node_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    fencing_token: Mapped[int | None] = mapped_column(Integer, nullable=True)
    process_identity: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    state: Mapped[str] = mapped_column(String(32), default="QUEUED")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class JobRow(Base):
    __tablename__ = "jobs"

    job_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))  # PREPARE / EXECUTE
    task_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    run_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    attempt_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    node_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    state: Mapped[str] = mapped_column(String(32), default="QUEUED", index=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    # 递增 fencing token 源（定义书 §数据库约束：递增 fencing_token）
    fencing_counter: Mapped[int] = mapped_column(Integer, default=0)
    row_version: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class LeaseRow(Base):
    __tablename__ = "leases"

    lease_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.job_id"), index=True)
    node_id: Mapped[str] = mapped_column(String(64))
    fencing_token: Mapped[int] = mapped_column(Integer)
    acquired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class EventRow(Base):
    __tablename__ = "events"
    __table_args__ = (
        # UNIQUE(job_id,event_seq)：事件序号唯一（定义书 §数据库约束）
        UniqueConstraint("job_id", "event_seq", name="uq_job_event_seq"),
    )

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.job_id"), index=True)
    event_seq: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(16))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ---------------------------------------------------------------------------
# Artifact / Bundle
# ---------------------------------------------------------------------------


class ArtifactRow(Base):
    __tablename__ = "artifacts"

    artifact_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(64), index=True)
    logical_path: Mapped[str] = mapped_column(String(512))
    length: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(16), default="TEMP")  # TEMP → COMMITTED
    job_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    run_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    attempt_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    evidence_mode: Mapped[str | None] = mapped_column(String(8), nullable=True)
    storage_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class BundleRow(Base):
    __tablename__ = "bundles"

    bundle_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.task_id"), index=True)
    revision: Mapped[int] = mapped_column(Integer)
    bundle_digest: Mapped[str] = mapped_column(String(64))
    manifest: Mapped[list] = mapped_column(JSON, default=list)
    validity: Mapped[str] = mapped_column(String(16), default="CURRENT")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    # 不可变性：Bundle 清单冻结，不可原位改写（定义书 §数据库约束）。
    # 强制见文件尾部 before_update 事件 + db/guards.py。


# ---------------------------------------------------------------------------
# Verification / Claim（Agent E 写入；此处只建表）
# ---------------------------------------------------------------------------


class VerificationRow(Base):
    __tablename__ = "verifications"

    verification_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.run_id"), index=True)
    attempt_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    rule_set_sha256: Mapped[str] = mapped_column(String(64))
    check_inputs: Mapped[dict] = mapped_column(JSON, default=dict)
    conclusion: Mapped[str] = mapped_column(String(32))
    findings: Mapped[list] = mapped_column(JSON, default=list)
    source_artifact_ids: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ClaimRow(Base):
    __tablename__ = "claims"

    claim_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    bundle_id: Mapped[str] = mapped_column(ForeignKey("bundles.bundle_id"), index=True)
    metric_id: Mapped[str] = mapped_column(String(128))
    artifact_id: Mapped[str] = mapped_column(String(64))
    text: Mapped[str] = mapped_column(Text)
    author_type: Mapped[str] = mapped_column(String(8))
    state: Mapped[str] = mapped_column(String(16), default="DRAFT")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ---------------------------------------------------------------------------
# 审查：Review / Issue / Reply / Decision
# ---------------------------------------------------------------------------


class ReviewRow(Base):
    __tablename__ = "reviews"

    review_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.task_id"), index=True)
    revision: Mapped[int] = mapped_column(Integer)
    bundle_id: Mapped[str] = mapped_column(ForeignKey("bundles.bundle_id"))
    bundle_digest: Mapped[str] = mapped_column(String(64))
    reviewer_id: Mapped[str] = mapped_column(String(128))
    state: Mapped[str] = mapped_column(String(32), default="PENDING")
    validity: Mapped[str] = mapped_column(String(16), default="CURRENT")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ReviewIssueRow(Base):
    __tablename__ = "issues"

    issue_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    review_id: Mapped[str] = mapped_column(ForeignKey("reviews.review_id"), index=True)
    created_by: Mapped[str] = mapped_column(String(128))
    responsible: Mapped[str] = mapped_column(String(128))
    severity: Mapped[str] = mapped_column(String(32))
    description: Mapped[str] = mapped_column(Text)
    close_criteria: Mapped[str] = mapped_column(Text)
    related_artifact_ids: Mapped[list] = mapped_column(JSON, default=list)
    related_run_ids: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(16), default="DRAFT")
    version: Mapped[int] = mapped_column(Integer, default=1)
    closed_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class IssueReplyRow(Base):
    __tablename__ = "issue_replies"

    reply_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    issue_id: Mapped[str] = mapped_column(ForeignKey("issues.issue_id"), index=True)
    author_id: Mapped[str] = mapped_column(String(128))
    body: Mapped[str] = mapped_column(Text)
    evidence_artifact_ids: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DecisionRow(Base):
    __tablename__ = "decisions"

    decision_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # 一个审查一个最终决定；决定对应固定 bundle（定义书 §数据库约束）
    review_id: Mapped[str] = mapped_column(
        ForeignKey("reviews.review_id"), unique=True, index=True
    )
    outcome: Mapped[str] = mapped_column(String(32))
    decided_by: Mapped[str] = mapped_column(String(128))
    task_id: Mapped[str] = mapped_column(String(64))
    revision: Mapped[int] = mapped_column(Integer)
    bundle_digest: Mapped[str] = mapped_column(String(64))
    purpose: Mapped[str] = mapped_column(String(64), default="design_screening")
    limitations: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    # 不可变性：已接受决定保持不可变（定义书 §接受后的历史），见 before_update 事件。


# ---------------------------------------------------------------------------
# 幂等 / 人工确认 / 能力包 / 节点
# ---------------------------------------------------------------------------


class IdempotencyRecordRow(Base):
    __tablename__ = "idempotency_records"
    __table_args__ = (
        # 主体+项目+action+key 唯一（CONVENTIONS §3.4）
        UniqueConstraint(
            "subject_id", "project_id", "action", "key", name="uq_idem_subject_project_action_key"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    subject_id: Mapped[str] = mapped_column(String(128))
    project_id: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(64))
    key: Mapped[str] = mapped_column(String(128))
    # 请求摘要、资源 ID 与响应摘要一起持久化（定义书 §数据库约束）
    request_sha256: Mapped[str] = mapped_column(String(64))
    resource_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    response_body: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    response_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class HumanConfirmationRow(Base):
    __tablename__ = "human_confirmations"

    confirmation_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    subject_id: Mapped[str] = mapped_column(String(128))
    action: Mapped[str] = mapped_column(String(64))
    target_id: Mapped[str] = mapped_column(String(64))
    target_digest: Mapped[str] = mapped_column(String(128))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CapabilityPackageRow(Base):
    __tablename__ = "capability_packages"
    __table_args__ = (
        UniqueConstraint("capability_package_id", "version", name="uq_cap_id_version"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    capability_package_id: Mapped[str] = mapped_column(String(128))
    version: Mapped[str] = mapped_column(String(32))
    manifest_sha256: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="DRAFT")
    purpose: Mapped[str] = mapped_column(String(64), default="design_screening")
    domain_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    compatibility: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class NodeRow(Base):
    __tablename__ = "nodes"

    node_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    capabilities: Mapped[dict] = mapped_column(JSON, default=dict)
    max_concurrent: Mapped[int] = mapped_column(Integer, default=1)
    registered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ---------------------------------------------------------------------------
# 不可变性硬守卫（数据库层事件，双保险；显式守卫见 db/guards.py）
# ---------------------------------------------------------------------------


@event.listens_for(TaskRevisionRow, "before_update")
def _revision_immutable(mapper, connection, target: TaskRevisionRow) -> None:  # noqa: ANN001
    """修订输入不可更新，仅新增（定义书 §数据库约束）。任何 UPDATE 一律拒绝。"""
    raise ApiError(
        ErrorCode.CONFLICT_REVISION,
        "TaskRevision 发布后不可原位修改；请创建新修订",
        details={"task_id": target.task_id, "revision": target.revision},
    )


@event.listens_for(BundleRow, "before_update")
def _bundle_manifest_immutable(mapper, connection, target: BundleRow) -> None:  # noqa: ANN001
    """Bundle 清单冻结：manifest/bundle_digest 不可原位改写；仅允许 validity 翻转
    （CURRENT→STALE，由新修订触发，属于"失效而非改写"）。"""
    state = target.__dict__
    insp = target._sa_instance_state
    for attr in ("manifest", "bundle_digest"):
        hist = insp.attrs[attr].history
        if hist.has_changes():
            raise ApiError(
                ErrorCode.CONFLICT_DIGEST,
                "Bundle manifest/bundle_digest 不可原位改写；请构建新 Bundle",
                details={"bundle_id": target.bundle_id},
            )


@event.listens_for(DecisionRow, "before_update")
def _decision_immutable(mapper, connection, target: DecisionRow) -> None:  # noqa: ANN001
    """已接受决定保持不可变；新输入使其失效而非删除（定义书 §接受后的历史）。"""
    raise ApiError(
        ErrorCode.CONFLICT_REVISION,
        "Decision 不可变；新输入使历史决定对当前输入失效，不修改原记录",
        details={"decision_id": target.decision_id},
    )


@event.listens_for(DecisionRow, "before_delete")
def _decision_no_delete(mapper, connection, target: DecisionRow) -> None:  # noqa: ANN001
    raise ApiError(
        ErrorCode.CONFLICT_REVISION,
        "历史决定不被删除（定义书 §任务、数值、适用与审查状态）",
        details={"decision_id": target.decision_id},
    )


__all__ = [
    "ArtifactRow",
    "AttemptRow",
    "AuthorizationRow",
    "Base",
    "BundleRow",
    "CapabilityPackageRow",
    "ClaimRow",
    "DecisionRow",
    "EventRow",
    "HumanConfirmationRow",
    "IdempotencyRecordRow",
    "IssueReplyRow",
    "JobRow",
    "LeaseRow",
    "NodeRow",
    "PreparationRow",
    "ReviewIssueRow",
    "ReviewRow",
    "RunRow",
    "TaskRevisionRow",
    "TaskRow",
    "VerificationRow",
    "utcnow",
]
