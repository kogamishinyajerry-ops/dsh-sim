"""runs 查询/取消/补算路由。"""
from __future__ import annotations

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
from dsh_sim.api.services import run_service
from dsh_sim.domain.identity import Identity

router = APIRouter()


@router.get("/runs/{run_id}", operation_id="getRun")
def getRun(
    run_id: str,
    after_seq: int = 0,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
) -> JSONResponse:
    run = run_service.get_run(session, identity, run_id, after_seq=after_seq)
    return JSONResponse(run.model_dump(mode="json"))


class CancelBody(BaseModel):
    reason: str | None = None


@router.post("/runs/{run_id}/cancel", operation_id="cancelRun")
def cancelRun(
    run_id: str,
    body: CancelBody | None = None,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
    idem: IdempotencyContext = Depends(idempotency("cancelRun")),
) -> JSONResponse:
    payload = {"run_id": run_id, **(body.model_dump(mode="json") if body else {})}
    stored = idem.lookup(payload)
    if (r := replay_or_none(stored)) is not None:
        return r
    run = run_service.cancel_run(session, identity, run_id, reason=(body.reason if body else None))
    return respond(idem, resource_id=run_id, status_code=202, body=run)


class RetryBody(BaseModel):
    reason: str
    recovery_strategy: str


@router.post("/runs/{run_id}/retry", operation_id="retryRun")
def retryRun(
    run_id: str,
    body: RetryBody,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
    idem: IdempotencyContext = Depends(idempotency("retryRun")),
) -> JSONResponse:
    payload = {"run_id": run_id, **body.model_dump(mode="json")}
    stored = idem.lookup(payload)
    if (r := replay_or_none(stored)) is not None:
        return r
    attempt = run_service.retry_run(
        session,
        identity,
        run_id,
        reason=body.reason,
        recovery_strategy=body.recovery_strategy,
    )
    return respond(idem, resource_id=attempt.attempt_id, status_code=202, body=attempt)


__all__ = ["router"]
