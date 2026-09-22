"""tasks / preparations / runs 提交类路由（operationId 与 OpenAPI 一字不差）。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from dsh_sim.api.deps import (
    IdempotencyContext,
    active_project,
    get_identity,
    get_session,
    idempotency,
)
from dsh_sim.api.routes._helpers import replay_or_none, respond
from dsh_sim.api.services import prep_service, run_service, task_service
from dsh_sim.domain.identity import Identity

router = APIRouter()


class CreateTaskBody(BaseModel):
    draft: dict[str, Any]


@router.post("/tasks", operation_id="createTask")
def createTask(
    body: CreateTaskBody,
    request: Request,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
    idem: IdempotencyContext = Depends(idempotency("createTask")),
) -> JSONResponse:
    stored = idem.lookup(body.model_dump(mode="json"))
    if (r := replay_or_none(stored)) is not None:
        return r
    task = task_service.create_task(session, identity, active_project(identity, request), body.draft)
    return respond(idem, resource_id=task.task_id, status_code=201, body=task)


class CreateRevisionBody(BaseModel):
    expected_revision: int
    spec: dict[str, Any]


@router.post("/tasks/{task_id}/revisions", operation_id="createRevision")
def createRevision(
    task_id: str,
    body: CreateRevisionBody,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
    idem: IdempotencyContext = Depends(idempotency("createRevision")),
) -> JSONResponse:
    payload = {"task_id": task_id, **body.model_dump(mode="json")}
    stored = idem.lookup(payload)
    if (r := replay_or_none(stored)) is not None:
        return r
    rev = task_service.create_revision(
        session, identity, task_id, expected_revision=body.expected_revision, spec=body.spec
    )
    return respond(idem, resource_id=task_id, status_code=201, body=rev)


class PrepareBody(BaseModel):
    revision: int


@router.post("/tasks/{task_id}/prepare", operation_id="prepareTask")
def prepareTask(
    task_id: str,
    body: PrepareBody,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
    idem: IdempotencyContext = Depends(idempotency("prepareTask")),
) -> JSONResponse:
    payload = {"task_id": task_id, **body.model_dump(mode="json")}
    stored = idem.lookup(payload)
    if (r := replay_or_none(stored)) is not None:
        return r
    preparation_id, job_id = prep_service.prepare_task(
        session, identity, task_id, revision=body.revision
    )
    return respond(
        idem,
        resource_id=preparation_id,
        status_code=202,
        body={"preparation_id": preparation_id, "job_id": job_id},
    )


@router.get("/preparations/{preparation_id}", operation_id="getPreparation")
def getPreparation(
    preparation_id: str,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
) -> JSONResponse:
    prep = prep_service.get_preparation(session, identity, preparation_id)
    return JSONResponse(prep.model_dump(mode="json"))


class AuthorizeBody(BaseModel):
    revision: int
    preparation_id: str
    prepared_digest: str
    execution_budget: dict[str, Any]
    confirmation_id: str


@router.post("/tasks/{task_id}/authorizations", operation_id="authorizeRuns")
def authorizeRuns(
    task_id: str,
    body: AuthorizeBody,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
    idem: IdempotencyContext = Depends(idempotency("authorizeRuns")),
) -> JSONResponse:
    payload = {"task_id": task_id, **body.model_dump(mode="json")}
    stored = idem.lookup(payload)
    if (r := replay_or_none(stored)) is not None:
        return r
    auth = run_service.authorize_runs(
        session,
        identity,
        task_id,
        revision=body.revision,
        preparation_id=body.preparation_id,
        prepared_digest=body.prepared_digest,
        execution_budget=body.execution_budget,
        confirmation_id=body.confirmation_id,
    )
    return respond(idem, resource_id=auth.authorization_id, status_code=201, body=auth)


class SubmitRunsBody(BaseModel):
    authorization_id: str
    prepared_digest: str


@router.post("/tasks/{task_id}/submissions", operation_id="submitRuns")
def submitRuns(
    task_id: str,
    body: SubmitRunsBody,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
    idem: IdempotencyContext = Depends(idempotency("submitRuns")),
) -> JSONResponse:
    payload = {"task_id": task_id, **body.model_dump(mode="json")}
    stored = idem.lookup(payload)
    if (r := replay_or_none(stored)) is not None:
        return r
    run_ids = run_service.submit_runs(
        session,
        identity,
        task_id,
        authorization_id=body.authorization_id,
        prepared_digest=body.prepared_digest,
    )
    return respond(
        idem, resource_id=task_id, status_code=202, body={"run_ids": run_ids}
    )


__all__ = ["router"]
