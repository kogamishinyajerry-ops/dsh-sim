"""证据包冻结（FR-17；定义书 §证据包、结论与可追溯性；WP-13）。

buildBundle 实现（接替 Agent C 的 BLOCKED 占位）：
- 冻结清单覆盖：TaskSpec、来源引用、模板与 prepared 摘要、真实回读、全部逻辑 Run
  及选用 attempt、原始 CSV/日志/.sim、指标、Verification、方法与规则版本、报告、Claim。
- 清单每项含 artifact 长度 + sha256；bundle_digest = canonical(manifest) 的 sha256；
  BundleRow 单次 INSERT（manifest 落库即冻结，db/models.py before_update 硬守卫禁止改写）。
- **缺必需工况不能靠减清单通过**：缺 Run/缺成功 attempt/缺原始 artifact 一律记入
  incomplete（bundle.summary.json + 审查门实时重算），并阻塞 ACCEPT（review/closeout.py）。
- READY_FOR_REVIEW 允许提交有缺项的诊断包；ACCEPT 受独立严格门控制。
"""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from dsh_sim.canonical import canonical_dumps, sha256_hex
from dsh_sim.db.models import (
    ArtifactRow,
    BundleRow,
    ClaimRow,
    PreparationRow,
    RunRow,
    TaskRevisionRow,
    TaskRow,
    VerificationRow,
)
from dsh_sim.domain.errors import ApiError, ErrorCode
from dsh_sim.verify.extract import extract_run_metrics
from dsh_sim.verify.verifier import load_metric_definitions


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:24]}"


def _stage_artifact(
    session: Session,
    *,
    project_id: str,
    logical_path: str,
    content: bytes,
    artifact_root: Path,
    evidence_mode: str | None,
) -> ArtifactRow:
    """写文件并登记 COMMITTED artifact（不 flush；由 build_bundle 统一提交）。"""
    sha = hashlib.sha256(content).hexdigest()
    artifact_id = _new_id("art")
    dest_dir = artifact_root / project_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / artifact_id
    tmp = dest_dir / f".{artifact_id}.tmp"
    tmp.write_bytes(content)
    tmp.replace(dest)  # 临时区→原子重命名
    row = ArtifactRow(
        artifact_id=artifact_id,
        project_id=project_id,
        logical_path=logical_path,
        length=len(content),
        sha256=sha,
        state="COMMITTED",
        evidence_mode=evidence_mode,
        storage_path=str(dest),
    )
    session.add(row)
    return row


def _json_bytes(obj: Any, *, mock: bool) -> bytes:
    banner = "# MOCK DATA - NOT REAL SOLVER OUTPUT\n" if mock else ""
    return (banner + json.dumps(obj, ensure_ascii=False, indent=1)).encode("utf-8")


def _entry(art: ArtifactRow, role: str) -> dict[str, Any]:
    return {
        "artifact_id": art.artifact_id,
        "logical_path": art.logical_path,
        "sha256": art.sha256,
        "length": art.length,
        "role": role,
    }


def bundle_evidence_mode(session: Session, task_id: str, revision: int) -> str:
    """包证据模式：任一 Run artifact 为 MOCK 则整包 MOCK（MOCK 绝不冒充 REAL）。"""
    run_ids = [
        r.run_id
        for r in session.query(RunRow).filter_by(task_id=task_id, revision=revision).all()
    ]
    if not run_ids:
        return "UNKNOWN"
    modes = [
        a.evidence_mode
        for a in session.query(ArtifactRow)
        .filter(ArtifactRow.run_id.in_(run_ids), ArtifactRow.state == "COMMITTED")
        .all()
    ]
    if any(m == "MOCK" for m in modes):
        return "MOCK"
    if modes and all(m == "REAL" for m in modes):
        return "REAL"
    return "UNKNOWN"


def compute_completeness(
    session: Session, task_id: str, revision: int
) -> dict[str, Any]:
    """必需工况完整性（缺一项即 incomplete；不能靠减少清单通过）。

    判定项：矩阵单元缺 Run / Run 当前 attempt 未 SUCCEEDED / 缺原始报告 artifact /
    缺 Verification 记录。确定性重算，供 buildBundle 与审查门共用。
    """
    rev = (
        session.query(TaskRevisionRow)
        .filter_by(task_id=task_id, revision=revision)
        .first()
    )
    missing: list[dict[str, Any]] = []
    if rev is None:
        return {"complete": False, "missing": [{"kind": "NO_REVISION", "revision": revision}]}
    spec = rev.spec
    runs = {
        (r.variant_id, r.condition_id): r
        for r in session.query(RunRow).filter_by(task_id=task_id, revision=revision).all()
    }
    for variant in spec["variants"]:
        for condition in spec["conditions"]:
            cell = f"{variant['variant_id']}×{condition['condition_id']}"
            run = runs.get((variant["variant_id"], condition["condition_id"]))
            if run is None:
                missing.append({"kind": "MISSING_RUN", "cell": cell})
                continue
            if run.execution_state != "SUCCEEDED":
                missing.append(
                    {
                        "kind": "RUN_NOT_SUCCEEDED",
                        "cell": cell,
                        "run_id": run.run_id,
                        "execution_state": run.execution_state,
                    }
                )
            artifacts = (
                session.query(ArtifactRow)
                .filter_by(run_id=run.run_id, state="COMMITTED")
                .all()
            )
            if not any("report" in a.logical_path for a in artifacts):
                missing.append(
                    {"kind": "MISSING_RAW_REPORT", "cell": cell, "run_id": run.run_id}
                )
            ver = (
                session.query(VerificationRow).filter_by(run_id=run.run_id).first()
            )
            if ver is None:
                missing.append(
                    {"kind": "MISSING_VERIFICATION", "cell": cell, "run_id": run.run_id}
                )
    return {"complete": not missing, "missing": missing}


def _draft_claims(
    session: Session, runs: list[RunRow], bundle_id: str
) -> list[ClaimRow]:
    """生成确定性 Claim：DRAFT → 数字与来源校验后 CONFIRMED（模型解释只是草稿）。

    Claim 数字取**独立重算值**（verify/extract 从原始报告复算）；
    与适配器提取器产物（metrics artifact）交叉一致才 CONFIRMED——
    证明提取与算术一致性（定义书 §原始数据与派生数据分开），不证明物理模型已验证。
    本函数只查询不入库（ClaimRow 由调用方在 BundleRow 之后统一 add，避免外键乱序）。
    """
    claims: list[ClaimRow] = []
    metric_defs = load_metric_definitions("buffer_chamber", "0.1.0")
    units = {m["metric_id"]: m.get("unit", "") for m in metric_defs.get("metrics", [])}
    for run in runs:
        artifacts = (
            session.query(ArtifactRow)
            .filter_by(run_id=run.run_id, state="COMMITTED")
            .all()
        )
        report = next((a for a in artifacts if "report" in a.logical_path), None)
        monitor = next((a for a in artifacts if "monitor" in a.logical_path), None)
        metrics_art = next((a for a in artifacts if "metrics" in a.logical_path), None)
        if metrics_art is None or not metrics_art.storage_path:
            continue
        body = Path(metrics_art.storage_path).read_text(encoding="utf-8")
        adapter_values: dict[str, Any] = json.loads(body.split("\n", 1)[-1]).get(
            "metric_values", {}
        )

        recomputed = extract_run_metrics(
            report.storage_path if report else None,
            monitor.storage_path if monitor else None,
            metric_definitions=metric_defs,
        ).metric_values

        for metric_id in sorted(set(adapter_values) | set(recomputed)):
            value = recomputed.get(metric_id)
            base = metric_id.split("@", 1)[0]
            unit = units.get(base, "")
            shown = "null（缺失）" if value is None else f"{value} {unit}".strip()
            cross = adapter_values.get(metric_id)
            confirmed = (
                value is not None
                and cross is not None
                and abs(float(value) - float(cross)) < 1e-12
            )
            claims.append(
                ClaimRow(
                    claim_id=_new_id("claim"),
                    bundle_id=bundle_id,
                    metric_id=metric_id,
                    artifact_id=metrics_art.artifact_id,
                    text=(
                        f"Run {run.variant_id}×{run.condition_id} 指标 {metric_id} = {shown}"
                        f"（独立复算值）；来源 artifact {metrics_art.artifact_id}。"
                    ),
                    author_type="AGENT",
                    state="CONFIRMED" if confirmed else "DRAFT",
                )
            )
    return claims


def build_bundle(
    session: Session,
    task_id: str,
    *,
    revision: int,
    artifact_root: str | Path,
) -> BundleRow:
    """构建冻结证据包。权限检查由调用方（api 服务层）完成。"""
    task = session.get(TaskRow, task_id)
    if task is None:
        raise ApiError(ErrorCode.VALIDATION, "任务不存在", details={"task_id": task_id})
    rev = (
        session.query(TaskRevisionRow)
        .filter_by(task_id=task_id, revision=revision)
        .first()
    )
    if rev is None:
        raise ApiError(ErrorCode.VALIDATION, "修订不存在", details={"revision": revision})

    artifact_root = Path(artifact_root)

    # 诚实守卫：无准备链产物且无任何 Run 时不构建"空包"（与 Agent C 原 BLOCKED
    # 占位语义一致——证据链未建立则显式阻塞，不补造 manifest）。
    prep_any = (
        session.query(PreparationRow)
        .filter_by(task_id=task_id, revision=revision)
        .first()
    )
    runs_any = (
        session.query(RunRow).filter_by(task_id=task_id, revision=revision).first()
    )
    if prep_any is None and runs_any is None:
        raise ApiError(
            ErrorCode.BLOCKED,
            "证据链未建立：该修订既无准备记录也无 Run，不能构建证据包（不补造 manifest）",
            retryable=False,
            details={"task_id": task_id, "revision": revision},
        )

    mode = bundle_evidence_mode(session, task_id, revision)
    mock = mode == "MOCK"
    infix = ".mock" if mock else ""
    mode_or_none = mode if mode != "UNKNOWN" else None
    completeness = compute_completeness(session, task_id, revision)

    bundle_id = _new_id("bundle")
    manifest: list[dict[str, Any]] = []
    base = f"bundles/{bundle_id}"

    def stage(logical: str, content: bytes, role: str, em: str | None = None) -> None:
        art = _stage_artifact(
            session,
            project_id=task.project_id,
            logical_path=logical,
            content=content,
            artifact_root=artifact_root,
            evidence_mode=em,
        )
        manifest.append(_entry(art, role))

    # 1) TaskSpec + 来源引用
    stage(
        f"{base}/task-spec{infix}.json",
        canonical_dumps(rev.spec).encode("utf-8"),
        "task_spec",
        mode_or_none,
    )
    stage(
        f"{base}/source-refs{infix}.json",
        _json_bytes({"source_refs": rev.source_refs, "spec_sha256": rev.spec_sha256}, mock=mock),
        "source_refs",
        mode_or_none,
    )
    # 2) 准备摘要 + 真实回读 + prepared 产物本体
    prep = (
        session.query(PreparationRow)
        .filter_by(task_id=task_id, revision=revision)
        .order_by(PreparationRow.created_at.desc())
        .first()
    )
    if prep is not None:
        stage(
            f"{base}/preparation{infix}.json",
            _json_bytes(
                {
                    "preparation_id": prep.preparation_id,
                    "prepared_digest": prep.prepared_digest,
                    "prepared_artifacts": prep.prepared_artifacts,
                    "readback_sha256": prep.readback_sha256,
                    "adapter_build": prep.adapter_build,
                    "software_build": prep.software_build,
                    "differences": prep.differences,
                },
                mock=mock,
            ),
            "preparation",
            mode_or_none,
        )
        for logical in prep.prepared_artifacts:
            art = (
                session.query(ArtifactRow)
                .filter_by(logical_path=logical, state="COMMITTED")
                .first()
            )
            if art is not None:
                manifest.append(_entry(art, "prepared_case"))
    # 3) 方法与规则版本（能力包文件原样冻结）
    pkg_root = Path(
        os.environ.get("DSH_SIM_CAPABILITIES_ROOT")
        or (Path(__file__).resolve().parents[3] / "capabilities")
    ) / "buffer_chamber" / "0.1.0"
    for name, role in (
        ("metric-definitions.json", "method_metrics"),
        ("rules.json", "rules"),
        ("domain.json", "method_domain"),
    ):
        p = pkg_root / name
        if p.is_file():
            stage(f"{base}/capability/{name}", p.read_bytes(), role, None)
    # 4) 全部逻辑 Run 及选用 attempt：原始 CSV/.sim/指标/Verification
    runs = (
        session.query(RunRow)
        .filter_by(task_id=task_id, revision=revision)
        .order_by(RunRow.variant_id, RunRow.condition_id)
        .all()
    )
    for run in runs:
        for art in (
            session.query(ArtifactRow)
            .filter_by(run_id=run.run_id, state="COMMITTED")
            .all()
        ):
            role = (
                "raw"
                if any(k in art.logical_path for k in ("report", "monitor", ".sim"))
                else "metrics"
            )
            manifest.append(_entry(art, role))
        for ver in session.query(VerificationRow).filter_by(run_id=run.run_id).all():
            stage(
                f"{base}/verifications/{run.run_id}/{ver.verification_id}{infix}.json",
                _json_bytes(
                    {
                        "verification_id": ver.verification_id,
                        "run_id": ver.run_id,
                        "attempt_id": ver.attempt_id,
                        "rule_set_sha256": ver.rule_set_sha256,
                        "check_inputs": ver.check_inputs,
                        "conclusion": ver.conclusion,
                        "findings": ver.findings,
                        "source_artifact_ids": ver.source_artifact_ids,
                    },
                    mock=mock,
                ),
                "verification",
                mode_or_none,
            )
    # 5) 摘要（含 incomplete 标记：缺项不能靠减清单通过）
    uncovered = completeness["missing"]
    stage(
        f"{base}/bundle.summary{infix}.json",
        _json_bytes(
            {
                "bundle_id": bundle_id,
                "task_id": task_id,
                "revision": revision,
                "evidence_mode": mode,
                "purpose": task.purpose,
                "complete": completeness["complete"],
                "incomplete_items": uncovered,
                "uncovered_scope": (
                    "全部必需工况齐备"
                    if completeness["complete"]
                    else f"缺 {len(uncovered)} 项（详见 incomplete_items）"
                ),
                "limitations": "仅限内部方案筛选用途；不构成适航符合性结论。",
            },
            mock=mock,
        ),
        "summary",
        mode_or_none,
    )
    # 6) 报告（清单摘要不含报告条目，报告内如实标注）
    from dsh_sim.evidence.report import render_report

    claims = _draft_claims(session, runs, bundle_id)
    pre_report_digest = sha256_hex(canonical_dumps(manifest))
    html = render_report(
        session,
        bundle_id=bundle_id,
        manifest_digest=pre_report_digest,
        task=task,
        rev=rev,
        runs=runs,
        claims=claims,
        evidence_mode=mode,
        completeness=completeness,
        manifest=manifest,
    )
    stage(f"{base}/report{infix}.html", html.encode("utf-8"), "report", mode_or_none)

    # 7) 单次 INSERT：manifest 定稿即冻结（before_update 守卫禁止后续改写）。
    # ClaimRow 在 BundleRow 之后统一 add（FK 排序：bundles → claims）。
    bundle = BundleRow(
        bundle_id=bundle_id,
        task_id=task_id,
        revision=revision,
        bundle_digest=sha256_hex(canonical_dumps(manifest)),
        manifest=manifest,
        validity="CURRENT",
    )
    session.add(bundle)
    for claim in claims:
        session.add(claim)
    session.flush()
    return bundle


__all__ = ["build_bundle", "bundle_evidence_mode", "compute_completeness"]
