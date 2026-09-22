"""nodes / jobs 路由：claimJob / postJobEvent（Worker 协议）。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
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
from dsh_sim.domain.errors import ApiError, ErrorCode
from dsh_sim.domain.identity import Identity, Role
from dsh_sim.domain.schemas import Job, Lease
from dsh_sim.domain.states import ExecutionState
from dsh_sim.queue import service as queue_service

router = APIRouter()


def _require_node_role(identity: Identity) -> None:
    """Worker 服务账户（节点级权限）；开发模式以 NODE_ADMIN 角色表达。"""
    if not identity.has_role(Role.NODE_ADMIN):
        raise ApiError(ErrorCode.FORBIDDEN, "claimJob/postJobEvent 需要节点服务身份（NODE_ADMIN）")


class ClaimBody(BaseModel):
    node_id: str
    node_capabilities: dict[str, Any] = {}


@router.post("/jobs/claim", operation_id="claimJob")
def claimJob(
    body: ClaimBody,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
    idem: IdempotencyContext = Depends(idempotency("claimJob")),
) -> JSONResponse:
    _require_node_role(identity)
    stored = idem.lookup(body.model_dump(mode="json"))
    if (r := replay_or_none(stored)) is not None:
        return r
    job, lease = queue_service.claim(
        session, node_id=body.node_id, capabilities=body.node_capabilities
    )
    payload: dict[str, Any] = {"job": None, "lease": None}
    if job is not None and lease is not None:
        payload = {
            "job": Job(
                job_id=job.job_id,
                kind=job.kind,
                task_id=job.task_id,
                run_id=job.run_id,
                attempt_id=job.attempt_id,
                node_id=job.node_id,
                state=ExecutionState(job.state),
                row_version=job.row_version,
                created_at=job.created_at,
            ).model_dump(mode="json"),
            "lease": Lease(
                lease_id=lease.lease_id,
                job_id=lease.job_id,
                node_id=lease.node_id,
                fencing_token=lease.fencing_token,
                acquired_at=lease.acquired_at,
                expires_at=lease.expires_at,
            ).model_dump(mode="json"),
        }
    return respond(
        idem,
        resource_id=job.job_id if job else None,
        status_code=200,
        body=payload,
    )


class EventBody(BaseModel):
    lease_id: str
    fencing_token: int
    event_seq: int
    kind: str
    payload: dict[str, Any] = {}


@router.post("/jobs/{job_id}/events", operation_id="postJobEvent")
def postJobEvent(
    job_id: str,
    body: EventBody,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
    idem: IdempotencyContext = Depends(idempotency("postJobEvent")),
) -> JSONResponse:
    _require_node_role(identity)
    payload = {"job_id": job_id, **body.model_dump(mode="json")}
    stored = idem.lookup(payload)
    if (r := replay_or_none(stored)) is not None:
        return r
    queue_service.post_event(
        session,
        job_id=job_id,
        lease_id=body.lease_id,
        fencing_token=body.fencing_token,
        event_seq=body.event_seq,
        kind=body.kind,
        payload=body.payload,
    )
    return respond(idem, resource_id=job_id, status_code=202, body={"accepted": True})


__all__ = ["router"]
