"""审查服务：buildBundle（BLOCKED 占位）/ getBundle / submitReview / getReview /
issues / decideReview。

强约束（定义书 §审查可以接受什么 / §工程API：审查、节点与上传）：
- ACCEPT 在数值 FAIL、证据 INSUFFICIENT、范围 UNCONFIRMED、MOCK 数据或未关闭阻塞时必须失败。
- decideReview 服务端必检：包摘要、修订有效性、必需 Run 完整性、各项检查、
  无未关闭阻塞、职责分离（执行者 ≠ 审查人）；Agent Bearer 始终拒绝。
- 模型创建的问题为 DRAFT，confirmIssue 人工确认转 OPEN；答复文本不能自动关闭问题。
"""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session

from dsh_sim.db.models import (
    ArtifactRow,
    BundleRow,
    DecisionRow,
    IssueReplyRow,
    ReviewIssueRow,
    ReviewRow,
    RunRow,
    utcnow,
)
from dsh_sim.domain.errors import ApiError, ErrorCode
from dsh_sim.domain.identity import Identity, Role
from dsh_sim.domain.schemas import (
    Bundle,
    BundleManifestEntry,
    Decision,
    IssueReply,
    Review,
    ReviewIssue,
)
from dsh_sim.domain.states import ReviewState, TaskFlowState, validate_transition

from dsh_sim.api.services.run_service import consume_confirmation
from dsh_sim.api.services.task_service import get_task_row, transition_task_flow


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:24]}"


# ---------------------------------------------------------------------------
# Bundle
# ---------------------------------------------------------------------------


def build_bundle(
    session: Session,
    identity: Identity,
    task_id: str,
    *,
    revision: int,
    artifact_root: str,
) -> Bundle:
    """证据包构建：委托 evidence/ 模块（Agent E 已交付 WP-13 冻结链）。

    本层只做权限检查与模型转换；冻结清单、必需 Run 完整性检查、报告生成
    全部在 evidence/bundle.py（缺必需工况标 incomplete，阻塞 ACCEPT）。"""
    get_task_row(session, identity, task_id)  # 权限检查先行
    from dsh_sim.evidence.bundle import build_bundle as _build

    row = _build(session, task_id, revision=revision, artifact_root=artifact_root)
    session.flush()
    return Bundle(
        bundle_id=row.bundle_id,
        task_id=row.task_id,
        revision=row.revision,
        bundle_digest=row.bundle_digest,
        manifest=[BundleManifestEntry(**{k: e[k] for k in ("artifact_id", "logical_path", "sha256")}) for e in row.manifest],
        validity=row.validity,
        created_at=row.created_at,
    )


def get_bundle(session: Session, identity: Identity, bundle_id: str) -> Bundle:
    bundle = session.get(BundleRow, bundle_id)
    if bundle is None:
        raise ApiError(ErrorCode.VALIDATION, "Bundle 不存在", details={"bundle_id": bundle_id})
    get_task_row(session, identity, bundle.task_id)
    return Bundle(
        bundle_id=bundle.bundle_id,
        task_id=bundle.task_id,
        revision=bundle.revision,
        bundle_digest=bundle.bundle_digest,
        manifest=[BundleManifestEntry(**e) for e in bundle.manifest],
        validity=bundle.validity,
        created_at=bundle.created_at,
    )


# ---------------------------------------------------------------------------
# Review
# ---------------------------------------------------------------------------


def submit_review(
    session: Session,
    identity: Identity,
    task_id: str,
    *,
    revision: int,
    bundle_id: str,
    bundle_digest: str,
    reviewer_id: str,
) -> Review:
    task = get_task_row(session, identity, task_id)
    bundle = session.get(BundleRow, bundle_id)
    if bundle is None or bundle.task_id != task_id:
        raise ApiError(ErrorCode.VALIDATION, "Bundle 不存在或不属于该任务")
    if bundle.bundle_digest != bundle_digest:
        raise ApiError(
            ErrorCode.CONFLICT_DIGEST,
            "bundle_digest 与冻结包摘要不一致",
            details={"bundle_id": bundle_id},
        )
    if bundle.validity != "CURRENT" or revision != task.current_revision or bundle.revision != revision:
        raise ApiError(
            ErrorCode.CONFLICT_REVISION,
            "只能提交当前修订的 CURRENT 冻结包",
            details={"revision": revision, "current": task.current_revision},
        )
    if reviewer_id == identity.subject_id:
        raise ApiError(
            ErrorCode.FORBIDDEN, "不能指定自己为审查人（职责分离，FR-22）"
        )
    if task.task_state not in (
        TaskFlowState.ACTIVE.value,
        TaskFlowState.READY_FOR_REVIEW.value,
    ):
        raise ApiError(
            ErrorCode.CONFLICT_REVISION,
            "任务未处于可提交审查阶段（ACTIVE/READY_FOR_REVIEW）",
            details={"task_state": task.task_state},
        )

    if task.task_state == TaskFlowState.ACTIVE.value:
        transition_task_flow(task, TaskFlowState.READY_FOR_REVIEW)
    transition_task_flow(task, TaskFlowState.IN_REVIEW)
    task.review_state = ReviewState.PENDING.value

    review = ReviewRow(
        review_id=_new_id("rev"),
        task_id=task_id,
        revision=revision,
        bundle_id=bundle_id,
        bundle_digest=bundle_digest,
        reviewer_id=reviewer_id,
        state=ReviewState.PENDING.value,
        validity="CURRENT",
    )
    session.add(review)
    session.flush()
    return _review_model(session, review)


def get_review(session: Session, identity: Identity, review_id: str) -> Review:
    review = session.get(ReviewRow, review_id)
    if review is None:
        raise ApiError(ErrorCode.VALIDATION, "审查不存在", details={"review_id": review_id})
    get_task_row(session, identity, review.task_id)
    return _review_model(session, review)


def _review_model(session: Session, review: ReviewRow) -> Review:
    issues = (
        session.query(ReviewIssueRow).filter_by(review_id=review.review_id).all()
    )
    return Review(
        review_id=review.review_id,
        task_id=review.task_id,
        revision=review.revision,
        bundle_id=review.bundle_id,
        bundle_digest=review.bundle_digest,
        reviewer_id=review.reviewer_id,
        state=ReviewState(review.state),
        validity=review.validity,
        issues=[_issue_model(session, i) for i in issues],
        created_at=review.created_at,
    )


def _issue_model(session: Session, issue: ReviewIssueRow) -> ReviewIssue:
    replies = (
        session.query(IssueReplyRow)
        .filter_by(issue_id=issue.issue_id)
        .order_by(IssueReplyRow.created_at)
        .all()
    )
    return ReviewIssue(
        issue_id=issue.issue_id,
        review_id=issue.review_id,
        created_by=issue.created_by,
        responsible=issue.responsible,
        severity=issue.severity,
        description=issue.description,
        close_criteria=issue.close_criteria,
        related_artifact_ids=issue.related_artifact_ids,
        related_run_ids=issue.related_run_ids,
        replies=[
            IssueReply(
                reply_id=r.reply_id,
                author_id=r.author_id,
                body=r.body,
                evidence_artifact_ids=r.evidence_artifact_ids,
                created_at=r.created_at,
            )
            for r in replies
        ],
        status=issue.status,
        version=issue.version,
        closed_by=issue.closed_by,
        closed_at=issue.closed_at,
        created_at=issue.created_at,
    )


# ---------------------------------------------------------------------------
# Issue
# ---------------------------------------------------------------------------


def create_issue(
    session: Session,
    identity: Identity,
    review_id: str,
    *,
    responsible: str,
    severity: str,
    description: str,
    close_criteria: str,
    related_artifact_ids: list[str] | None = None,
    related_run_ids: list[str] | None = None,
) -> ReviewIssue:
    review = session.get(ReviewRow, review_id)
    if review is None:
        raise ApiError(ErrorCode.VALIDATION, "审查不存在", details={"review_id": review_id})
    get_task_row(session, identity, review.task_id)
    issue = ReviewIssueRow(
        issue_id=_new_id("issue"),
        review_id=review_id,
        created_by=identity.subject_id,
        responsible=responsible,
        severity=severity,
        description=description,
        close_criteria=close_criteria,
        related_artifact_ids=related_artifact_ids or [],
        related_run_ids=related_run_ids or [],
        status="DRAFT",  # 模型创建/人工创建均为 DRAFT；confirmIssue 人工确认转 OPEN
        version=1,
    )
    session.add(issue)
    session.flush()
    return _issue_model(session, issue)


def _get_issue(session: Session, identity: Identity, issue_id: str) -> ReviewIssueRow:
    issue = session.get(ReviewIssueRow, issue_id)
    if issue is None:
        raise ApiError(ErrorCode.VALIDATION, "问题不存在", details={"issue_id": issue_id})
    review = session.get(ReviewRow, issue.review_id)
    get_task_row(session, identity, review.task_id)
    return issue


def reply_issue(
    session: Session,
    identity: Identity,
    issue_id: str,
    *,
    body: str,
    evidence_artifact_ids: list[str] | None = None,
) -> ReviewIssue:
    """回复问题并附新增证据；答复文本本身不能自动关闭问题（FR-21）。"""
    issue = _get_issue(session, identity, issue_id)
    if issue.status == "CLOSED":
        raise ApiError(ErrorCode.CONFLICT_REVISION, "问题已关闭，不能继续回复")
    reply = IssueReplyRow(
        reply_id=_new_id("reply"),
        issue_id=issue_id,
        author_id=identity.subject_id,
        body=body,
        evidence_artifact_ids=evidence_artifact_ids or [],
    )
    session.add(reply)
    issue.version += 1
    session.flush()
    return _issue_model(session, issue)


def confirm_issue(
    session: Session,
    identity: Identity,
    issue_id: str,
    *,
    issue_version: int,
    confirmation_id: str,
) -> ReviewIssue:
    """指定审查人人工确认 DRAFT→OPEN；模型接口无此工具。"""
    identity.require_human()
    issue = _get_issue(session, identity, issue_id)
    review = session.get(ReviewRow, issue.review_id)
    _require_reviewer(identity, review)
    if issue.version != issue_version:
        raise ApiError(
            ErrorCode.CONFLICT_REVISION,
            "issue_version 与当前版本不一致",
            details={"expected": issue_version, "current": issue.version},
        )
    if issue.status != "DRAFT":
        raise ApiError(ErrorCode.CONFLICT_REVISION, "仅 DRAFT 问题可确认转 OPEN")
    consume_confirmation(
        session,
        identity,
        confirmation_id=confirmation_id,
        action="confirmIssue",
        target_id=issue_id,
        target_digest=review.bundle_digest,
    )
    issue.status = "OPEN"
    issue.version += 1
    session.flush()
    return _issue_model(session, issue)


def close_issue(
    session: Session,
    identity: Identity,
    issue_id: str,
    *,
    issue_version: int,
    close_evidence_artifact_ids: list[str],
    confirmation_id: str,
) -> ReviewIssue:
    """审查人核对回复与新增证据后关闭；一次性人工确认。Agent 不能关闭问题。"""
    identity.require_human()
    issue = _get_issue(session, identity, issue_id)
    review = session.get(ReviewRow, issue.review_id)
    _require_reviewer(identity, review)
    if issue.version != issue_version:
        raise ApiError(
            ErrorCode.CONFLICT_REVISION,
            "issue_version 与当前版本不一致",
            details={"expected": issue_version, "current": issue.version},
        )
    if issue.status != "OPEN":
        raise ApiError(ErrorCode.CONFLICT_REVISION, "仅 OPEN 问题可关闭")
    if not close_evidence_artifact_ids:
        raise ApiError(
            ErrorCode.ENGINEERING_INPUT, "关闭问题必须附关闭证据 artifact（关闭需审查人确认+证据）"
        )
    consume_confirmation(
        session,
        identity,
        confirmation_id=confirmation_id,
        action="closeIssue",
        target_id=issue_id,
        target_digest=review.bundle_digest,
    )
    issue.status = "CLOSED"
    issue.closed_by = identity.subject_id
    issue.closed_at = utcnow()
    issue.version += 1
    session.flush()
    return _issue_model(session, issue)


def _require_reviewer(identity: Identity, review: ReviewRow) -> None:
    if identity.subject_id != review.reviewer_id or not identity.has_role(Role.REVIEWER):
        raise ApiError(
            ErrorCode.FORBIDDEN, "仅指定审查人可执行该动作", details={"review_id": review.review_id}
        )


# ---------------------------------------------------------------------------
# Decision
# ---------------------------------------------------------------------------


def decide_review(
    session: Session,
    identity: Identity,
    review_id: str,
    *,
    outcome: str,
    bundle_digest: str,
    revision: int,
    limitations: str | None,
    confirmation_id: str,
) -> Decision:
    """人工决定。ACCEPT 门（定义书 §审查可以接受什么）在事务内全部复检。"""
    identity.require_human()  # Agent 代签拒绝
    review = session.get(ReviewRow, review_id)
    if review is None:
        raise ApiError(ErrorCode.VALIDATION, "审查不存在", details={"review_id": review_id})
    task = get_task_row(session, identity, review.task_id)
    _require_reviewer(identity, review)

    # 职责分离：执行者 ≠ 审查人（FR-22 不可自行验收）
    if identity.subject_id == task.owner_id:
        raise ApiError(
            ErrorCode.FORBIDDEN, "职责分离：执行者不能验收本人任务"
        )
    if review.state != ReviewState.PENDING.value:
        raise ApiError(
            ErrorCode.CONFLICT_REVISION,
            "审查不处于 PENDING，不能决定",
            details={"state": review.state},
        )
    if review.validity != "CURRENT" or revision != task.current_revision:
        raise ApiError(
            ErrorCode.CONFLICT_REVISION,
            "修订有效性检查失败：决定必须绑定当前修订（与新修订并发时只有合法一方生效）",
            details={"requested": revision, "current": task.current_revision},
        )
    if review.bundle_digest != bundle_digest:
        raise ApiError(
            ErrorCode.CONFLICT_DIGEST, "bundle_digest 与审查绑定摘要不一致"
        )

    open_issues = (
        session.query(ReviewIssueRow)
        .filter(ReviewIssueRow.review_id == review_id, ReviewIssueRow.status.in_(["DRAFT", "OPEN"]))
        .all()
    )
    blocking_open = [i for i in open_issues if i.status == "OPEN"]

    runs = (
        session.query(RunRow)
        .filter_by(task_id=review.task_id, revision=review.revision)
        .all()
    )

    if outcome == "ACCEPT":
        if blocking_open:
            raise ApiError(
                ErrorCode.BLOCKED,
                "存在未关闭阻塞问题，ACCEPT 必须失败",
                details={"open_issue_ids": [i.issue_id for i in blocking_open]},
            )
        failed = [r.run_id for r in runs if r.numerical_state == "FAIL"]
        insufficient = [r.run_id for r in runs if r.numerical_state == "INSUFFICIENT"]
        unconfirmed = [r.run_id for r in runs if r.applicability_state == "UNCONFIRMED"]
        not_succeeded = [r.run_id for r in runs if r.execution_state != "SUCCEEDED"]
        mock_artifacts = (
            session.query(ArtifactRow)
            .filter(
                ArtifactRow.run_id.in_([r.run_id for r in runs] or ["-"]),
                ArtifactRow.evidence_mode == "MOCK",
            )
            .all()
        )
        problems: dict[str, Any] = {}
        if failed:
            problems["numerical_fail"] = failed
        if insufficient:
            problems["numerical_insufficient"] = insufficient
        if unconfirmed:
            problems["applicability_unconfirmed"] = unconfirmed
        if not_succeeded:
            problems["execution_not_succeeded"] = not_succeeded
        if mock_artifacts:
            problems["mock_evidence"] = [a.artifact_id for a in mock_artifacts]
        # 包维度阻塞（Agent E review/closeout.py）：STALE / incomplete / DRAFT 未确认
        from dsh_sim.review.closeout import bundle_blockers

        problems.update(bundle_blockers(session, review))
        if problems:
            raise ApiError(
                ErrorCode.BLOCKED,
                "存在数值FAIL/证据INSUFFICIENT/范围UNCONFIRMED/MOCK/未完成 Run，ACCEPT 必须失败；"
                "仍允许 REQUEST_CHANGES 或 REJECT",
                details=problems,
            )

    consume_confirmation(
        session,
        identity,
        confirmation_id=confirmation_id,
        action="decideReview",
        target_id=review_id,
        target_digest=bundle_digest,
    )

    decision = DecisionRow(
        decision_id=_new_id("dec"),
        review_id=review_id,
        outcome=outcome,
        decided_by=identity.subject_id,
        task_id=review.task_id,
        revision=review.revision,
        bundle_digest=bundle_digest,
        purpose=task.purpose,
        limitations=limitations,
    )
    session.add(decision)

    state_map = {
        "ACCEPT": (ReviewState.ACCEPTED, TaskFlowState.ACCEPTED),
        "REQUEST_CHANGES": (ReviewState.CHANGES_REQUESTED, TaskFlowState.CHANGES_REQUESTED),
        "REJECT": (ReviewState.REJECTED, TaskFlowState.REJECTED),
    }
    review_target, task_target = state_map[outcome]
    validate_transition(ReviewState(review.state), review_target)
    review.state = review_target.value
    task.review_state = review_target.value
    transition_task_flow(task, task_target)
    session.flush()

    return Decision(
        decision_id=decision.decision_id,
        review_id=review_id,
        outcome=outcome,
        decided_by=decision.decided_by,
        task_id=decision.task_id,
        revision=decision.revision,
        bundle_digest=bundle_digest,
        purpose=decision.purpose,
        limitations=limitations,
        decided_at=decision.decided_at,
    )


__all__ = [
    "build_bundle",
    "close_issue",
    "confirm_issue",
    "create_issue",
    "decide_review",
    "get_bundle",
    "get_review",
    "reply_issue",
    "submit_review",
]
