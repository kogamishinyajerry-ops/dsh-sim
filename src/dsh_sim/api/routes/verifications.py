"""verifications 路由（Agent E / FR-15/FR-20）：独立复算 recheck + 记录读取。

recheck：审查台"独立复算"按钮的服务端入口——从该 Run 已提交原始 artifact
重新提取并复算，产生新 Verification 记录（旧记录不原位改写，定义书 §修订）。
"""
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
from dsh_sim.api.routes.projections import _ver_json
from dsh_sim.api.services.task_service import get_task_row
from dsh_sim.db.models import ArtifactRow, RunRow, VerificationRow
from dsh_sim.domain.errors import ApiError, ErrorCode
from dsh_sim.domain.identity import Identity
from dsh_sim.verify.extract import extract_run_metrics
from dsh_sim.verify.verifier import (
    load_domain,
    load_metric_definitions,
    load_rule_set,
    persist_verification,
    verify_run,
)

router = APIRouter()


class RecheckBody(BaseModel):
    run_id: str


@router.post("/verifications/recheck", operation_id="recheckVerification")
def recheck(
    body: RecheckBody,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
    idem: IdempotencyContext = Depends(idempotency("recheckVerification")),
) -> JSONResponse:
    stored = idem.lookup(body.model_dump(mode="json"))
    if (r := replay_or_none(stored)) is not None:
        return r
    run = session.get(RunRow, body.run_id)
    if run is None:
        raise ApiError(ErrorCode.VALIDATION, "Run 不存在", details={"run_id": body.run_id})
    get_task_row(session, identity, run.task_id)

    arts = (
        session.query(ArtifactRow)
        .filter_by(run_id=run.run_id, state="COMMITTED")
        .all()
    )
    report = next((a for a in arts if "report" in a.logical_path), None)
    monitor = next((a for a in arts if "monitor" in a.logical_path), None)
    if report is None:
        raise ApiError(
            ErrorCode.BLOCKED,
            "无原始报告 artifact，无法独立复算（NOT_RUN，不补造）",
            details={"run_id": run.run_id},
        )

    rules, rule_set_sha = load_rule_set("buffer_chamber", "0.1.0")
    metric_defs = load_metric_definitions("buffer_chamber", "0.1.0")
    domain = load_domain("buffer_chamber", "0.1.0")
    extracted = extract_run_metrics(
        report.storage_path,
        monitor.storage_path if monitor else None,
        metric_definitions=metric_defs,
    )
    result = verify_run(extracted=extracted, rules=rules, domain=domain)
    ver = persist_verification(
        session,
        run=run,
        attempt_id=run.current_attempt_id,
        result=result,
        rule_set_sha256=rule_set_sha,
        source_artifact_ids=[a.artifact_id for a in (report, monitor) if a],
    )
    session.flush()
    return respond(
        idem, resource_id=ver.verification_id, status_code=201, body=_ver_json(ver)
    )


@router.get("/verifications", operation_id="listVerifications")
def list_verifications(
    task_id: str | None = None,
    run_id: str | None = None,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
) -> JSONResponse:
    if run_id:
        run = session.get(RunRow, run_id)
        if run is None:
            raise ApiError(ErrorCode.VALIDATION, "Run 不存在", details={"run_id": run_id})
        get_task_row(session, identity, run.task_id)
        rows = (
            session.query(VerificationRow)
            .filter_by(run_id=run_id)
            .order_by(VerificationRow.created_at)
            .all()
        )
    elif task_id:
        get_task_row(session, identity, task_id)
        run_ids = [r.run_id for r in session.query(RunRow).filter_by(task_id=task_id)]
        rows = (
            session.query(VerificationRow)
            .filter(VerificationRow.run_id.in_(run_ids or ["-"]))
            .order_by(VerificationRow.created_at)
            .all()
        )
    else:
        raise ApiError(ErrorCode.VALIDATION, "必须提供 task_id 或 run_id 查询参数")
    return JSONResponse({"items": [_ver_json(v) for v in rows]})


__all__ = ["router"]
