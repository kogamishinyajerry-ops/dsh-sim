"""verifications 路由（Agent E / FR-15/FR-20）：独立复算 recheck + 记录读取。

recheck：审查台"独立复算"按钮的服务端入口——从该 Run 已提交原始 artifact
重新提取并复算，产生新 Verification 记录（旧记录不原位改写，定义书 §修订）。
"""
from __future__ import annotations

import hashlib
from pathlib import Path, PurePosixPath

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
from dsh_sim.capabilities.registry import resolve_method_package
from dsh_sim.db.models import ArtifactRow, RunRow, TaskRevisionRow, VerificationRow
from dsh_sim.domain.errors import ApiError, ErrorCode
from dsh_sim.domain.identity import Identity
from dsh_sim.verify.extract import check_unit_semantics, extract_run_metrics
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
    task = get_task_row(session, identity, run.task_id)
    if run.current_attempt_id is None:
        raise ApiError(ErrorCode.BLOCKED, "Run 没有当前 attempt，无法选择复算证据")
    revision = session.query(TaskRevisionRow).filter_by(task_id=run.task_id, revision=run.revision).one_or_none()
    if revision is None:
        raise ApiError(ErrorCode.BLOCKED, "Run 的任务修订不存在，不能猜测复算方法")

    arts = (
        session.query(ArtifactRow)
        .filter_by(project_id=task.project_id, run_id=run.run_id,
                   attempt_id=run.current_attempt_id, state="COMMITTED")
        .all()
    )
    def select(names: set[str]) -> ArtifactRow | None:
        matched = [a for a in arts if PurePosixPath(a.logical_path).name in names]
        if len(matched) > 1:
            raise ApiError(ErrorCode.BLOCKED, "当前 attempt 有多个同类规范产物，无法唯一绑定复算输入",
                           details={"artifact_ids": [a.artifact_id for a in matched]})
        return matched[0] if matched else None

    report = select({"report.csv", "report.mock.csv"})
    monitor = select({"monitor.csv", "monitor.mock.csv"})
    if report is None:
        raise ApiError(
            ErrorCode.BLOCKED,
            "无原始报告 artifact，无法独立复算（NOT_RUN，不补造）",
            details={"run_id": run.run_id},
        )

    sources = [a for a in (report, monitor) if a is not None]
    modes = {a.evidence_mode for a in sources}
    if modes not in ({"REAL"}, {"MOCK"}):
        raise ApiError(ErrorCode.BLOCKED, "原始复算证据模式未知或混合，不能假定 REAL")
    mode = next(iter(modes))
    for art in sources:
        if not art.storage_path or not Path(art.storage_path).is_file():
            raise ApiError(ErrorCode.BLOCKED, "原始复算文件缺失", details={"artifact_id": art.artifact_id})
        data = Path(art.storage_path).read_bytes()
        if len(data) != art.length or hashlib.sha256(data).hexdigest() != art.sha256:
            raise ApiError(ErrorCode.BLOCKED, "原始复算文件摘要或长度不符", details={"artifact_id": art.artifact_id})
    method = revision.spec.get("method") or {}
    resolved = resolve_method_package(session, method)
    exact = resolved.digest_match and resolved.manifest_sha256 == resolved.declared_sha256
    if mode == "REAL" and not exact:
        raise ApiError(ErrorCode.BLOCKED, "REAL 复算方法包摘要与 TaskSpec 不一致，不能替换版本")
    rules, rule_set_sha = load_rule_set(resolved.capability_package_id, resolved.version)
    metric_defs = load_metric_definitions(resolved.capability_package_id, resolved.version)
    domain = load_domain(resolved.capability_package_id, resolved.version)
    extracted = extract_run_metrics(
        report.storage_path,
        monitor.storage_path if monitor else None,
        metric_definitions=metric_defs,
    )
    extracted.findings.extend(check_unit_semantics(revision.spec))
    result = verify_run(extracted=extracted, rules=rules, domain=domain,
                        required_metrics=method.get("required_metrics", []))
    result.check_inputs.update({"evidence_mode": mode, "method_version": resolved.version,
                                "method_digest_match": exact, "mock_method_fixture": mode == "MOCK" and not exact})
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
