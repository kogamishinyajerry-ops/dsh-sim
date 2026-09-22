"""artifacts 路由：registerArtifact / uploadArtifactContent / getArtifactContent。

上传流程（定义书 §上传与大文件）：注册（TEMP）→ 受权 PUT → 全量长度/哈希核验 →
临时区原子重命名 → 数据库标记 COMMITTED。文件已写而事务失败属可回收孤儿；
事务完成却文件不可读属证据故障。
"""
from __future__ import annotations

import hashlib
import os
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from dsh_sim.api.deps import (
    IdempotencyContext,
    get_identity,
    get_session,
    idempotency,
)
from dsh_sim.api.routes._helpers import replay_or_none, respond
from dsh_sim.api.services.task_service import get_task_row
from dsh_sim.db.models import ArtifactRow, JobRow, TaskRow
from dsh_sim.domain.errors import ApiError, ErrorCode
from dsh_sim.domain.identity import Identity
from dsh_sim.domain.schemas import Artifact

router = APIRouter()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:24]}"


class RegisterBody(BaseModel):
    job_id: str
    logical_path: str
    length: int
    sha256: str
    run_id: str | None = None
    attempt_id: str | None = None
    evidence_mode: str | None = None  # REAL / MOCK；缺省 REAL（Mock 数据必须显式声明）


def _job_project(session: Session, job_id: str) -> tuple[JobRow, str]:
    job = session.get(JobRow, job_id)
    if job is None:
        raise ApiError(ErrorCode.VALIDATION, "job 不存在", details={"job_id": job_id})
    if job.task_id is None:
        raise ApiError(
            ErrorCode.VALIDATION, "附件必须属于带任务归属的当前 Job", details={"job_id": job_id}
        )
    task = session.get(TaskRow, job.task_id)
    return job, task.project_id


@router.post("/artifacts", operation_id="registerArtifact")
def registerArtifact(
    body: RegisterBody,
    request: Request,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
    idem: IdempotencyContext = Depends(idempotency("registerArtifact")),
) -> JSONResponse:
    stored = idem.lookup(body.model_dump(mode="json"))
    if (r := replay_or_none(stored)) is not None:
        return r
    job, project_id = _job_project(session, body.job_id)
    if not identity.can_access_project(project_id):
        raise ApiError(ErrorCode.FORBIDDEN, "跨项目写入被拒绝")
    # 非配属节点不能写入：开发模式以 X-Dev-Node-Id 头表达节点身份
    node_id = request.headers.get("x-dev-node-id")
    if job.node_id is not None and node_id is not None and node_id != job.node_id:
        raise ApiError(ErrorCode.FORBIDDEN, "非配属节点不能写入该 Job 的附件")

    artifact = ArtifactRow(
        artifact_id=_new_id("art"),
        project_id=project_id,
        logical_path=body.logical_path,
        length=body.length,
        sha256=body.sha256,
        state="TEMP",
        job_id=body.job_id,
        run_id=body.run_id,
        attempt_id=body.attempt_id,
        evidence_mode=body.evidence_mode or "REAL",
    )
    session.add(artifact)
    session.flush()
    upload_url = f"/api/v1/artifacts/{artifact.artifact_id}/content"
    return respond(
        idem,
        resource_id=artifact.artifact_id,
        status_code=201,
        body={"artifact_id": artifact.artifact_id, "upload_url": upload_url},
    )


def _artifact_model(a: ArtifactRow) -> Artifact:
    return Artifact(
        artifact_id=a.artifact_id,
        project_id=a.project_id,
        logical_path=a.logical_path,
        length=a.length,
        sha256=a.sha256,
        state=a.state,
        job_id=a.job_id,
        run_id=a.run_id,
        attempt_id=a.attempt_id,
        evidence_mode=a.evidence_mode,
        created_at=a.created_at,
    )


@router.put("/artifacts/{artifact_id}/content", operation_id="uploadArtifactContent")
async def uploadArtifactContent(
    artifact_id: str,
    request: Request,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
) -> JSONResponse:
    artifact = session.get(ArtifactRow, artifact_id)
    if artifact is None:
        raise ApiError(ErrorCode.VALIDATION, "附件不存在", details={"artifact_id": artifact_id})
    if not identity.can_access_project(artifact.project_id):
        raise ApiError(ErrorCode.FORBIDDEN, "跨项目写入被拒绝")
    if artifact.state == "COMMITTED":
        raise ApiError(
            ErrorCode.CONFLICT_DIGEST, "附件已提交，不可覆盖（先临时后核验，提交后不可改）"
        )
    job = session.get(JobRow, artifact.job_id) if artifact.job_id else None
    node_id = request.headers.get("x-dev-node-id")
    if job is not None and job.node_id is not None and node_id is not None and node_id != job.node_id:
        raise ApiError(ErrorCode.FORBIDDEN, "非配属节点不能写入")

    content = await request.body()
    # 全量长度/哈希核验（定义书 §上传与大文件）
    actual_sha = hashlib.sha256(content).hexdigest()
    if len(content) != artifact.length or actual_sha != artifact.sha256:
        raise ApiError(
            ErrorCode.CONFLICT_DIGEST,
            "上传内容长度/摘要与登记不一致",
            details={
                "expected_length": artifact.length,
                "actual_length": len(content),
            },
        )

    root: Path = request.app.state.artifact_root
    project_dir = root / artifact.project_id
    project_dir.mkdir(parents=True, exist_ok=True)
    final_path = project_dir / artifact.artifact_id
    tmp_path = project_dir / f".{artifact.artifact_id}.tmp"
    tmp_path.write_bytes(content)
    os.replace(tmp_path, final_path)  # 临时区→原子重命名

    artifact.storage_path = str(final_path)
    artifact.state = "COMMITTED"
    session.flush()
    return JSONResponse(_artifact_model(artifact).model_dump(mode="json"))


@router.get("/artifacts/{artifact_id}/content", operation_id="getArtifactContent")
def getArtifactContent(
    artifact_id: str,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
) -> FileResponse:
    artifact = session.get(ArtifactRow, artifact_id)
    if artifact is None:
        raise ApiError(ErrorCode.VALIDATION, "附件不存在", details={"artifact_id": artifact_id})
    # 每次读取验证主体与项目权限；不接受任意主机路径（仅按 artifact_id 定位受控存储）
    if not identity.can_access_project(artifact.project_id):
        raise ApiError(ErrorCode.FORBIDDEN, "跨项目读取被拒绝")
    if artifact.state != "COMMITTED" or not artifact.storage_path:
        raise ApiError(
            ErrorCode.BLOCKED,
            "附件未提交或文件缺失（证据故障须阻塞，不冒充可读）",
            details={"artifact_id": artifact_id},
        )
    path = Path(artifact.storage_path)
    if not path.is_file():
        raise ApiError(
            ErrorCode.BLOCKED,
            "已提交附件的文件不可读：证据故障，必须阻塞接受",
            details={"artifact_id": artifact_id},
        )
    return FileResponse(path, filename=Path(artifact.logical_path).name)


__all__ = ["router"]
