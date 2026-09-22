"""读取投影路由（Agent E / FR-29 两张工作台硬依赖；纯增量，x-extension: read-projection）。

只读投影：不承载业务动作；所有响应带 evidence_mode（MOCK/REAL/UNKNOWN，
UNKNOWN 表示尚无证据产物——不冒充 REAL）。
blocker 统一形状 {code,message,responsible,action}。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from dsh_sim.api.deps import get_identity, get_session
from dsh_sim.api.services.task_service import get_task_row
from dsh_sim.db.models import (
    ArtifactRow,
    BundleRow,
    ClaimRow,
    EventRow,
    JobRow,
    PreparationRow,
    RunRow,
    TaskRevisionRow,
    TaskRow,
    VerificationRow,
)
from dsh_sim.domain.errors import ApiError, ErrorCode
from dsh_sim.domain.identity import Identity
from dsh_sim.evidence.bundle import bundle_evidence_mode, compute_completeness
from dsh_sim.review.closeout import review_history
from dsh_sim.verify.verifier import load_metric_definitions

router = APIRouter()

# 任务列表筛选（定义书 §执行工作台：需要我处理/运行中/待审查/历史）
_FILTERS: dict[str, tuple[str, ...]] = {
    "need_action": ("DRAFT", "PREPARING", "READY", "CHANGES_REQUESTED"),
    "running": ("AUTHORIZED", "ACTIVE"),
    "pending_review": ("READY_FOR_REVIEW", "IN_REVIEW"),
    "history": ("ACCEPTED", "REJECTED"),
}


def _normalize_blockers(raw: list[dict[str, Any]]) -> list[dict[str, str]]:
    """统一 {code,message,responsible,action}；原始键不同时尽力映射，不丢信息。"""
    out: list[dict[str, str]] = []
    for b in raw or []:
        if {"code", "message", "responsible", "action"} <= set(b):
            out.append({k: str(b[k]) for k in ("code", "message", "responsible", "action")})
        elif "question" in b:  # open_questions 形状
            out.append(
                {
                    "code": f"OPEN_QUESTION:{b.get('field', '?')}",
                    "message": str(b.get("question", "")),
                    "responsible": str(b.get("responsible", "")),
                    "action": "补全缺失输入后创建新修订",
                }
            )
        else:  # 占位/其他形状：原样保留于 message
            out.append(
                {
                    "code": str(b.get("kind") or b.get("code") or "BLOCKED"),
                    "message": str(b.get("detail") or b.get("message") or json.dumps(b, ensure_ascii=False)),
                    "responsible": str(b.get("responsible", "")),
                    "action": str(b.get("action", "")),
                }
            )
    return out


def _task_json(session: Session, task: TaskRow) -> dict[str, Any]:
    return {
        "task_id": task.task_id,
        "project_id": task.project_id,
        "current_revision": task.current_revision,
        "task_state": task.task_state,
        "review_state": task.review_state,
        "owner_id": task.owner_id,
        "purpose": task.purpose,
        "blockers": task.blockers,
        "blocker_objects": _normalize_blockers(task.blockers),
        "evidence_mode": bundle_evidence_mode(session, task.task_id, task.current_revision)
        if task.current_revision >= 1
        else "UNKNOWN",
        "created_at": task.created_at.isoformat(),
    }


@router.get("/tasks", operation_id="listTaskProjection")
def list_tasks(
    filter: str | None = None,
    cursor: str | None = None,
    limit: int = 50,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
) -> JSONResponse:
    limit = max(1, min(limit, 200))
    offset = int(cursor) if cursor else 0
    q = session.query(TaskRow).filter(TaskRow.project_id.in_(sorted(identity.project_ids)))
    if filter:
        if filter not in _FILTERS:
            raise ApiError(
                ErrorCode.VALIDATION,
                f"未知筛选 {filter!r}（允许 {sorted(_FILTERS)}）",
            )
        q = q.filter(TaskRow.task_state.in_(_FILTERS[filter]))
    rows = q.order_by(TaskRow.created_at.desc()).offset(offset).limit(limit + 1).all()
    items = [_task_json(session, t) for t in rows[:limit]]
    next_cursor = str(offset + limit) if len(rows) > limit else None
    return JSONResponse({"items": items, "next_cursor": next_cursor})


@router.get("/tasks/{task_id}", operation_id="getTaskProjection")
def get_task(
    task_id: str,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
) -> JSONResponse:
    task = get_task_row(session, identity, task_id)
    return JSONResponse(_task_json(session, task))


@router.get("/tasks/{task_id}/revisions/latest", operation_id="getLatestRevisionProjection")
def get_latest_revision(
    task_id: str,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
) -> JSONResponse:
    task = get_task_row(session, identity, task_id)
    rev = (
        session.query(TaskRevisionRow)
        .filter_by(task_id=task_id, revision=task.current_revision)
        .first()
    )
    if rev is None:
        raise ApiError(
            ErrorCode.BLOCKED, "任务尚无修订（NOT_RUN）", details={"task_id": task_id}
        )
    return JSONResponse(
        {
            "task_id": task_id,
            "revision": rev.revision,
            "spec": rev.spec,
            "spec_sha256": rev.spec_sha256,
            "source_refs": rev.source_refs,
            "created_by": rev.created_by,
            "created_at": rev.created_at.isoformat(),
        }
    )


@router.get("/tasks/{task_id}/runs", operation_id="listRunProjection")
def list_runs(
    task_id: str,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
) -> JSONResponse:
    task = get_task_row(session, identity, task_id)
    runs = (
        session.query(RunRow)
        .filter_by(task_id=task_id, revision=task.current_revision)
        .order_by(RunRow.variant_id, RunRow.condition_id)
        .all()
    )
    items: list[dict[str, Any]] = []
    for run in runs:
        job_ids = [j.job_id for j in session.query(JobRow).filter_by(run_id=run.run_id).all()]
        events = (
            session.query(EventRow)
            .filter(EventRow.job_id.in_(job_ids))
            .order_by(EventRow.event_seq)
            .all()
            if job_ids
            else []
        )
        last = events[-1] if events else None
        items.append(
            {
                "run_id": run.run_id,
                "task_id": run.task_id,
                "revision": run.revision,
                "variant_id": run.variant_id,
                "condition_id": run.condition_id,
                "execution_state": run.execution_state,
                "numerical_state": run.numerical_state,
                "applicability_state": run.applicability_state,
                "current_attempt_id": run.current_attempt_id,
                "latest_stage": (last.payload or {}).get("stage") if last else None,
                "latest_event_at": last.occurred_at.isoformat() if last else None,
                "events": [
                    {
                        "event_id": e.event_id,
                        "job_id": e.job_id,
                        "event_seq": e.event_seq,
                        "kind": e.kind,
                        "payload": e.payload,
                        "occurred_at": e.occurred_at.isoformat(),
                    }
                    for e in events
                ],
                "created_at": run.created_at.isoformat(),
            }
        )
    return JSONResponse({"items": items})


@router.get("/tasks/{task_id}/events", operation_id="listTaskEventsIncremental")
def list_task_events(
    task_id: str,
    after_seq: int = 0,
    limit: int = 200,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
) -> JSONResponse:
    """跨 job 增量事件流（x-extension: read-projection；加性变更）。

    该任务全部 job 的事件按 (occurred_at, event_seq) 全局排序后重编号 global_seq；
    只返回 global_seq > after_seq 的（after_seq 填上次响应的 next_after 做增量）。
    """
    limit = max(1, min(limit, 200))
    get_task_row(session, identity, task_id)
    # 跨 job 合并：EXECUTE job 经 run→task 归属；PREPARE job 直接带 task_id
    all_job_ids = {
        j.job_id
        for j in session.query(JobRow).filter(JobRow.task_id == task_id).all()
    } | {
        j.job_id
        for j in session.query(JobRow)
        .join(RunRow, RunRow.run_id == JobRow.run_id)
        .filter(RunRow.task_id == task_id)
        .all()
    }
    events = (
        session.query(EventRow)
        .filter(EventRow.job_id.in_(sorted(all_job_ids) or ["-"]))
        .order_by(EventRow.occurred_at, EventRow.event_seq)
        .all()
    )
    items: list[dict[str, Any]] = []
    for global_seq, e in enumerate(events, start=1):
        if global_seq <= after_seq:
            continue
        if len(items) >= limit:
            break
        items.append(
            {
                "global_seq": global_seq,
                "job_id": e.job_id,
                "event_seq": e.event_seq,
                "kind": e.kind,
                "payload": e.payload,
                "occurred_at": e.occurred_at.isoformat(),
            }
        )
    # next_after = 本次返回的最大 global_seq（无新事件保持 after_seq，客户端原样重发）
    next_after = items[-1]["global_seq"] if items else after_seq
    return JSONResponse({"items": items, "next_after": next_after})


@router.get("/tasks/{task_id}/preparations/latest", operation_id="getLatestPreparationProjection")
def get_latest_preparation(
    task_id: str,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
) -> JSONResponse:
    task = get_task_row(session, identity, task_id)
    prep = (
        session.query(PreparationRow)
        .filter_by(task_id=task_id, revision=task.current_revision)
        .order_by(PreparationRow.created_at.desc())
        .first()
    )
    if prep is None:
        raise ApiError(
            ErrorCode.BLOCKED, "任务尚无准备记录（NOT_RUN）", details={"task_id": task_id}
        )
    # differences 归一：field/requested/readback/unit/boundary/status（面板确认区硬依赖）
    differences = [
        {
            "field": d.get("field", ""),
            "requested": d.get("requested"),
            "readback": d.get("readback"),
            "unit": d.get("unit", ""),
            "boundary": d.get("boundary", ""),
            "status": d.get("status", "MISMATCH"),
        }
        for d in (prep.differences or [])
    ]
    return JSONResponse(
        {
            "preparation_id": prep.preparation_id,
            "task_id": prep.task_id,
            "revision": prep.revision,
            "prepared_digest": prep.prepared_digest,
            "prepared_artifacts": prep.prepared_artifacts,
            "readback_sha256": prep.readback_sha256,
            "adapter_build": prep.adapter_build,
            "software_build": prep.software_build,
            "ready": prep.ready,
            "differences": differences,
            "blockers": prep.blockers,
            "blocker_objects": _normalize_blockers(prep.blockers),
            "evidence_mode": bundle_evidence_mode(session, task_id, prep.revision),
            "created_at": prep.created_at.isoformat(),
        }
    )


def _bundle_json(session: Session, bundle: BundleRow) -> dict[str, Any]:
    completeness = compute_completeness(session, bundle.task_id, bundle.revision)
    return {
        "bundle_id": bundle.bundle_id,
        "task_id": bundle.task_id,
        "revision": bundle.revision,
        "bundle_digest": bundle.bundle_digest,
        "manifest": bundle.manifest,
        "validity": bundle.validity,
        "evidence_mode": bundle_evidence_mode(session, bundle.task_id, bundle.revision),
        "complete": completeness["complete"],
        "incomplete_items": completeness["missing"],
        "uncovered_scope": (
            "全部必需工况齐备"
            if completeness["complete"]
            else f"缺 {len(completeness['missing'])} 项（详见 incomplete_items）"
        ),
        "limitations": "仅限内部方案筛选用途；不构成适航符合性结论。",
        "created_at": bundle.created_at.isoformat(),
    }


@router.get("/tasks/{task_id}/bundles/latest", operation_id="getLatestBundleProjection")
def get_latest_bundle(
    task_id: str,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
) -> JSONResponse:
    task = get_task_row(session, identity, task_id)
    bundle = (
        session.query(BundleRow)
        .filter_by(task_id=task_id)
        .order_by(BundleRow.created_at.desc())
        .first()
    )
    if bundle is None:
        raise ApiError(
            ErrorCode.BLOCKED, "任务尚无证据包（NOT_RUN）", details={"task_id": task_id}
        )
    return JSONResponse(_bundle_json(session, bundle))


def _get_bundle_checked(session: Session, identity: Identity, bundle_id: str) -> BundleRow:
    bundle = session.get(BundleRow, bundle_id)
    if bundle is None:
        raise ApiError(ErrorCode.VALIDATION, "Bundle 不存在", details={"bundle_id": bundle_id})
    get_task_row(session, identity, bundle.task_id)
    return bundle


@router.get("/bundles/{bundle_id}/manifest", operation_id="getBundleManifestProjection")
def get_bundle_projection(
    bundle_id: str,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
) -> JSONResponse:
    """全量冻结清单投影：manifest 含 role/length（契约 getBundle 响应为 3 字段引用形，
    全量清单走本投影，x-extension: read-projection 加性增量）。"""
    return JSONResponse(_bundle_json(session, _get_bundle_checked(session, identity, bundle_id)))


@router.get("/bundles/{bundle_id}/claims", operation_id="listBundleClaims")
def list_claims(
    bundle_id: str,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
) -> JSONResponse:
    _get_bundle_checked(session, identity, bundle_id)
    claims = (
        session.query(ClaimRow)
        .filter_by(bundle_id=bundle_id)
        .order_by(ClaimRow.metric_id)
        .all()
    )
    return JSONResponse(
        {
            "items": [
                {
                    "claim_id": c.claim_id,
                    "bundle_id": c.bundle_id,
                    "metric_id": c.metric_id,
                    "artifact_id": c.artifact_id,
                    "text": c.text,
                    "author_type": c.author_type,
                    "state": c.state,
                    "created_at": c.created_at.isoformat(),
                }
                for c in claims
            ]
        }
    )


@router.get("/bundles/{bundle_id}/metrics", operation_id="listBundleMetrics")
def list_metrics(
    bundle_id: str,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
) -> JSONResponse:
    bundle = _get_bundle_checked(session, identity, bundle_id)
    units = {
        m["metric_id"]: m.get("unit", "")
        for m in load_metric_definitions("buffer_chamber", "0.1.0").get("metrics", [])
    }
    items: list[dict[str, Any]] = []
    runs = (
        session.query(RunRow)
        .filter_by(task_id=bundle.task_id, revision=bundle.revision)
        .order_by(RunRow.variant_id, RunRow.condition_id)
        .all()
    )
    for run in runs:
        arts = (
            session.query(ArtifactRow)
            .filter_by(run_id=run.run_id, state="COMMITTED")
            .all()
        )
        metrics_art = next((a for a in arts if "metrics" in a.logical_path), None)
        seen: set[str] = set()
        if metrics_art is not None and metrics_art.storage_path:
            body = Path(metrics_art.storage_path).read_text(encoding="utf-8")
            payload = json.loads(body.split("\n", 1)[-1])
            # 适配器固定提取器给出的原始量（ADR-09 双源之一）
            for metric_id, value in sorted((payload.get("metric_values") or {}).items()):
                seen.add(metric_id)
                items.append(
                    {
                        "metric_id": metric_id,
                        "variant_id": run.variant_id,
                        "condition_id": run.condition_id,
                        "run_id": run.run_id,
                        "value": value,  # 缺值保持 null，不填 0
                        "unit": units.get(metric_id.split("@", 1)[0], ""),
                        "artifact_id": metrics_art.artifact_id,
                        "source": "adapter_extract",
                        "missing": value is None,
                    }
                )
        # 独立复算派生指标（ADR-09 双源之二，来源=最新 Verification.check_inputs）
        ver = (
            session.query(VerificationRow)
            .filter_by(run_id=run.run_id)
            .order_by(VerificationRow.created_at.desc())
            .first()
        )
        if ver is not None:
            derived = (ver.check_inputs or {}).get("metric_values") or {}
            for metric_id, value in sorted(derived.items()):
                if metric_id in seen:
                    continue  # 原始量不重复列；双源一致性由 Claim 交叉确认
                items.append(
                    {
                        "metric_id": metric_id,
                        "variant_id": run.variant_id,
                        "condition_id": run.condition_id,
                        "run_id": run.run_id,
                        "value": value,
                        "unit": units.get(metric_id.split("@", 1)[0], ""),
                        "artifact_id": (ver.source_artifact_ids or [None])[0],
                        "source": "independent_recompute",
                        "missing": value is None,
                    }
                )
    return JSONResponse({"items": items})


@router.get("/bundles/{bundle_id}/verifications", operation_id="listBundleVerifications")
def list_bundle_verifications(
    bundle_id: str,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
) -> JSONResponse:
    bundle = _get_bundle_checked(session, identity, bundle_id)
    run_ids = [
        r.run_id
        for r in session.query(RunRow).filter_by(
            task_id=bundle.task_id, revision=bundle.revision
        )
    ]
    vers = (
        session.query(VerificationRow)
        .filter(VerificationRow.run_id.in_(run_ids or ["-"]))
        .order_by(VerificationRow.created_at)
        .all()
    )
    return JSONResponse({"items": [_ver_json(v) for v in vers]})


def _ver_json(v: VerificationRow) -> dict[str, Any]:
    return {
        "verification_id": v.verification_id,
        "run_id": v.run_id,
        "attempt_id": v.attempt_id,
        "rule_set_sha256": v.rule_set_sha256,
        "check_inputs": v.check_inputs,
        "conclusion": v.conclusion,
        "findings": v.findings,
        "source_artifact_ids": v.source_artifact_ids,
        "created_at": v.created_at.isoformat(),
    }


@router.get("/tasks/{task_id}/review-history", operation_id="getReviewHistoryProjection")
def get_review_history(
    task_id: str,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
) -> JSONResponse:
    """历史两态（FR-23）："当时接受 R1"与"当前 R2 尚未接受"可同时读取。"""
    get_task_row(session, identity, task_id)
    return JSONResponse({"items": review_history(session, task_id)})


__all__ = ["router"]
