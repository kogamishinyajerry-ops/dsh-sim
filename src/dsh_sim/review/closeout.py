"""审查闭环收尾（FR-19..23；定义书 §审查可以接受什么 / §修订、整改与历史批准）。

decide_review 阻塞检查的"包维度"补全（run 维度检查已在 api/services/review_service.py）：
- 包 STALE（新修订生效后旧包失效，FR-23）；
- 包 incomplete（缺必需工况不能靠减清单通过，缺就标 incomplete 并阻塞 ACCEPT）；
- DRAFT 问题未确认（DRAFT 不是已关闭；答复文本不能自动关闭问题）。

历史两态（定义书 §接受后的历史）：review_history 返回跨修订的审查/决定记录，
界面可同时展示"当时接受 R1"与"当前 R2 尚未接受"；历史决定不可变不删除。
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from dsh_sim.db.models import (
    BundleRow,
    DecisionRow,
    ReviewIssueRow,
    ReviewRow,
)
from dsh_sim.domain.errors import ApiError
from dsh_sim.evidence.bundle import (
    compute_completeness_manifest,
    manifest_snapshot,
    verify_bundle_integrity,
)
from dsh_sim.review.acceptance import run_acceptance_blockers


def bundle_blockers(session: Session, review: ReviewRow) -> dict[str, Any]:
    """ACCEPT 门的包维度阻塞（并入 decide_review 的 problems，逐条列因）。

    上游验收报告问题 1（2026-09-22）：冻结包是验收对象，不能被后来的补算"补绿"。
    验收输入一律取**冻结快照**（manifest 裁剪），而不是当前 Run/attempt 全集：
    - 完整性按 manifest 集合重算（冻结后新证据不入旧包的 completeness）；
    - digest 与文件字节按冻结值核对（改一字节/来源缺失即阻塞）；
    - 正向门（run/attempt 证据）同样只看冻结集合内对象。
    """
    problems: dict[str, Any] = {}
    bundle = session.get(BundleRow, review.bundle_id)
    if bundle is None:
        problems["bundle_missing"] = [review.bundle_id]
        return problems
    if bundle.validity != "CURRENT":
        # 新修订生效 → 旧 bundle STALE；旧包不能继续批准新修订（FR-23）
        problems["bundle_stale"] = [
            {"bundle_id": bundle.bundle_id, "validity": bundle.validity}
        ]
    # 冻结包完整性：digest 绑定 + 字节级校验（问题 1 核心）
    problems.update(verify_bundle_integrity(session, bundle))
    # 验收输入裁剪到冻结集合；来源缺失（manifest 引用 artifact 不存在）直接阻塞
    try:
        runs, artifacts, verifications = manifest_snapshot(session, bundle)
    except ApiError as exc:
        problems["bundle_source_missing"] = exc.details.get(
            "missing_artifact_ids", [review.bundle_id]
        )
        return problems
    completeness = compute_completeness_manifest(
        session, bundle, runs, artifacts, verifications
    )
    if not completeness["complete"]:
        problems["bundle_incomplete"] = completeness["missing"]
    # Positive gates: NOT_CHECKED, OUT_OF_SCOPE, UNKNOWN and previous-attempt
    # evidence cannot slip through the legacy negative-state checks.
    problems.update(run_acceptance_blockers(
        [
            {name: getattr(run, name) for name in (
                "run_id", "current_attempt_id", "execution_state",
                "numerical_state", "applicability_state",
            )}
            for run in runs
        ],
        [
            {name: getattr(art, name) for name in (
                "artifact_id", "run_id", "attempt_id", "state", "evidence_mode",
            )}
            for art in artifacts
        ],
        [
            {name: check.get(name) for name in (
                "verification_id", "run_id", "attempt_id", "conclusion",
                "source_artifact_ids",
            )}
            for check in verifications
        ],
    ))
    drafts = (
        session.query(ReviewIssueRow)
        .filter(
            ReviewIssueRow.review_id == review.review_id,
            ReviewIssueRow.status == "DRAFT",
        )
        .all()
    )
    if drafts:
        problems["draft_issues_unconfirmed"] = [i.issue_id for i in drafts]
    return problems


def blocker_shape(code: str, message: str, responsible: str, action: str) -> dict[str, str]:
    """面板阻塞项统一形状 {code,message,responsible,action}。"""
    return {"code": code, "message": message, "responsible": responsible, "action": action}


def review_history(session: Session, task_id: str) -> list[dict[str, Any]]:
    """跨修订审查/决定历史（新输入使历史决定失效而非删除；全部可读）。"""
    reviews = (
        session.query(ReviewRow)
        .filter_by(task_id=task_id)
        .order_by(ReviewRow.revision, ReviewRow.created_at)
        .all()
    )
    out: list[dict[str, Any]] = []
    for r in reviews:
        decision = (
            session.query(DecisionRow).filter_by(review_id=r.review_id).first()
        )
        out.append(
            {
                "review_id": r.review_id,
                "revision": r.revision,
                "state": r.state,
                "validity": r.validity,
                "bundle_digest": r.bundle_digest,
                "reviewer_id": r.reviewer_id,
                "decision": (
                    {
                        "decision_id": decision.decision_id,
                        "outcome": decision.outcome,
                        "decided_by": decision.decided_by,
                        "decided_at": decision.decided_at.isoformat(),
                        "purpose": decision.purpose,
                        "limitations": decision.limitations,
                    }
                    if decision
                    else None
                ),
                "created_at": r.created_at.isoformat(),
            }
        )
    return out


__all__ = ["blocker_shape", "bundle_blockers", "review_history"]
