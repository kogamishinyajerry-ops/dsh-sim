"""bundles / reviews / issues / confirmations / capabilities 路由。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from dsh_sim.api.deps import (
    IdempotencyContext,
    get_identity,
    get_session,
    idempotency,
)
from dsh_sim.api.routes._helpers import replay_or_none, respond
from dsh_sim.api.services import review_service, run_service
from dsh_sim.db.models import CapabilityPackageRow
from dsh_sim.domain.identity import Identity
from dsh_sim.domain.schemas import CapabilityPackage

router = APIRouter()


# ----------------------------- capabilities -----------------------------


@router.get("/capabilities", operation_id="listCapabilities")
def listCapabilities(
    cursor: str | None = None,
    limit: int = 50,
    status: str = "RELEASED",
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
) -> JSONResponse:
    limit = max(1, min(limit, 200))
    offset = int(cursor) if cursor else 0
    query = session.query(CapabilityPackageRow)
    if status != "ANY":
        # 默认只有 RELEASED 能力可用于正式任务（定义书 §能力包与最小知识体系）；
        # status=DRAFT/ANY 仅用于目录管理查看，不解除使用门。
        query = query.filter(CapabilityPackageRow.status == status)
    rows = query.order_by(CapabilityPackageRow.id).offset(offset).limit(limit + 1).all()
    items = [
        CapabilityPackage(
            capability_package_id=r.capability_package_id,
            version=r.version,
            manifest_sha256=r.manifest_sha256,
            status=r.status,
            purpose=r.purpose,
            domain_summary=r.domain_summary,
            compatibility=r.compatibility,
        ).model_dump(mode="json")
        for r in rows[:limit]
    ]
    next_cursor = str(offset + limit) if len(rows) > limit else None
    return JSONResponse({"items": items, "next_cursor": next_cursor})


# ----------------------------- bundles -----------------------------


class BuildBundleBody(BaseModel):
    revision: int


@router.post("/tasks/{task_id}/bundles", operation_id="buildBundle")
def buildBundle(
    task_id: str,
    body: BuildBundleBody,
    request: Request,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
    idem: IdempotencyContext = Depends(idempotency("buildBundle")),
) -> JSONResponse:
    payload = {"task_id": task_id, **body.model_dump(mode="json")}
    stored = idem.lookup(payload)
    if (r := replay_or_none(stored)) is not None:
        return r
    # 证据冻结链（Agent E 已交付 WP-13）：同步构建冻结包；
    # 无准备/无 Run 时服务层仍显式 BLOCKED（503，retryable=false）
    bundle = review_service.build_bundle(
        session,
        identity,
        task_id,
        revision=body.revision,
        artifact_root=str(request.app.state.artifact_root),
    )
    return respond(idem, resource_id=bundle.bundle_id, status_code=201, body=bundle)


@router.get("/bundles/{bundle_id}", operation_id="getBundle")
def getBundle(
    bundle_id: str,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
) -> JSONResponse:
    bundle = review_service.get_bundle(session, identity, bundle_id)
    return JSONResponse(bundle.model_dump(mode="json"))


# ----------------------------- reviews -----------------------------


class SubmitReviewBody(BaseModel):
    revision: int
    bundle_id: str
    bundle_digest: str
    reviewer_id: str


@router.post("/tasks/{task_id}/reviews", operation_id="submitReview")
def submitReview(
    task_id: str,
    body: SubmitReviewBody,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
    idem: IdempotencyContext = Depends(idempotency("submitReview")),
) -> JSONResponse:
    payload = {"task_id": task_id, **body.model_dump(mode="json")}
    stored = idem.lookup(payload)
    if (r := replay_or_none(stored)) is not None:
        return r
    review = review_service.submit_review(
        session,
        identity,
        task_id,
        revision=body.revision,
        bundle_id=body.bundle_id,
        bundle_digest=body.bundle_digest,
        reviewer_id=body.reviewer_id,
    )
    return respond(idem, resource_id=review.review_id, status_code=201, body=review)


@router.get("/reviews/{review_id}", operation_id="getReview")
def getReview(
    review_id: str,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
) -> JSONResponse:
    review = review_service.get_review(session, identity, review_id)
    return JSONResponse(review.model_dump(mode="json"))


class DecideBody(BaseModel):
    outcome: str  # ACCEPT / REQUEST_CHANGES / REJECT
    bundle_digest: str
    revision: int
    limitations: str | None = None
    confirmation_id: str


@router.post("/reviews/{review_id}/decisions", operation_id="decideReview")
def decideReview(
    review_id: str,
    body: DecideBody,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
    idem: IdempotencyContext = Depends(idempotency("decideReview")),
) -> JSONResponse:
    payload = {"review_id": review_id, **body.model_dump(mode="json")}
    stored = idem.lookup(payload)
    if (r := replay_or_none(stored)) is not None:
        return r
    decision = review_service.decide_review(
        session,
        identity,
        review_id,
        outcome=body.outcome,
        bundle_digest=body.bundle_digest,
        revision=body.revision,
        limitations=body.limitations,
        confirmation_id=body.confirmation_id,
    )
    return respond(idem, resource_id=decision.decision_id, status_code=201, body=decision)


# ----------------------------- issues -----------------------------


class CreateIssueBody(BaseModel):
    responsible: str
    severity: str
    description: str
    close_criteria: str
    related_artifact_ids: list[str] | None = None
    related_run_ids: list[str] | None = None


@router.post("/reviews/{review_id}/issues", operation_id="createIssue")
def createIssue(
    review_id: str,
    body: CreateIssueBody,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
    idem: IdempotencyContext = Depends(idempotency("createIssue")),
) -> JSONResponse:
    payload = {"review_id": review_id, **body.model_dump(mode="json")}
    stored = idem.lookup(payload)
    if (r := replay_or_none(stored)) is not None:
        return r
    issue = review_service.create_issue(
        session,
        identity,
        review_id,
        responsible=body.responsible,
        severity=body.severity,
        description=body.description,
        close_criteria=body.close_criteria,
        related_artifact_ids=body.related_artifact_ids,
        related_run_ids=body.related_run_ids,
    )
    return respond(idem, resource_id=issue.issue_id, status_code=201, body=issue)


class ReplyBody(BaseModel):
    body: str
    evidence_artifact_ids: list[str] | None = None


@router.post("/issues/{issue_id}/replies", operation_id="replyIssue")
def replyIssue(
    issue_id: str,
    body: ReplyBody,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
    idem: IdempotencyContext = Depends(idempotency("replyIssue")),
) -> JSONResponse:
    payload = {"issue_id": issue_id, **body.model_dump(mode="json")}
    stored = idem.lookup(payload)
    if (r := replay_or_none(stored)) is not None:
        return r
    issue = review_service.reply_issue(
        session, identity, issue_id, body=body.body, evidence_artifact_ids=body.evidence_artifact_ids
    )
    return respond(idem, resource_id=issue_id, status_code=201, body=issue)


class ConfirmIssueBody(BaseModel):
    issue_version: int
    confirmation_id: str


@router.post("/issues/{issue_id}/confirm", operation_id="confirmIssue")
def confirmIssue(
    issue_id: str,
    body: ConfirmIssueBody,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
    idem: IdempotencyContext = Depends(idempotency("confirmIssue")),
) -> JSONResponse:
    payload = {"issue_id": issue_id, **body.model_dump(mode="json")}
    stored = idem.lookup(payload)
    if (r := replay_or_none(stored)) is not None:
        return r
    issue = review_service.confirm_issue(
        session,
        identity,
        issue_id,
        issue_version=body.issue_version,
        confirmation_id=body.confirmation_id,
    )
    return respond(idem, resource_id=issue_id, status_code=200, body=issue)


class CloseIssueBody(BaseModel):
    issue_version: int
    close_evidence_artifact_ids: list[str]
    confirmation_id: str


@router.post("/issues/{issue_id}/close", operation_id="closeIssue")
def closeIssue(
    issue_id: str,
    body: CloseIssueBody,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
    idem: IdempotencyContext = Depends(idempotency("closeIssue")),
) -> JSONResponse:
    payload = {"issue_id": issue_id, **body.model_dump(mode="json")}
    stored = idem.lookup(payload)
    if (r := replay_or_none(stored)) is not None:
        return r
    issue = review_service.close_issue(
        session,
        identity,
        issue_id,
        issue_version=body.issue_version,
        close_evidence_artifact_ids=body.close_evidence_artifact_ids,
        confirmation_id=body.confirmation_id,
    )
    return respond(idem, resource_id=issue_id, status_code=200, body=issue)


# ----------------------------- confirmations -----------------------------


class ConfirmationBody(BaseModel):
    action: str
    target_id: str
    target_digest: str


@router.post("/confirmations", operation_id="createHumanConfirmation")
def createHumanConfirmation(
    body: ConfirmationBody,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
    idem: IdempotencyContext = Depends(idempotency("createHumanConfirmation")),
) -> JSONResponse:
    stored = idem.lookup(body.model_dump(mode="json"))
    if (r := replay_or_none(stored)) is not None:
        return r
    confirmation_id, expires_at = run_service.issue_confirmation(
        session,
        identity,
        action=body.action,
        target_id=body.target_id,
        target_digest=body.target_digest,
    )
    return respond(
        idem,
        resource_id=confirmation_id,
        status_code=201,
        body={"confirmation_id": confirmation_id, "expires_at": expires_at.isoformat()},
    )


__all__ = ["router"]
