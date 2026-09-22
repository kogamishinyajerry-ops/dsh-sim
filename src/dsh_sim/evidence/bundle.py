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
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from dsh_sim.canonical import canonical_dumps, sha256_hex
from dsh_sim.capabilities.registry import resolve_method_package
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
    """包证据模式：上游（准备产物）与 Run 产物取**最弱环**，MOCK 绝不冒充 REAL。

    上游报告问题 2：整体模式此前只依据 Run 产物，准备/模板等上游 MOCK 来源
    不参与污染传播——"Mock 准备 + REAL-tagged 合成 Run" 的夹具会被推断成
    全链 REAL。现把准备产物（run_id=NULL、evidence_mode=MOCK）按归属一并计入：

    - 任一来源为 MOCK → 整包 MOCK；
    - 全部来源为 REAL → REAL；
    - 其余（含 UNKNOWN / NULL）→ UNKNOWN，绝不被推断成 REAL。
    """
    task = session.get(TaskRow, task_id)
    project_id = task.project_id if task is not None else None

    modes: list[str | None] = []
    run_ids = [
        r.run_id
        for r in session.query(RunRow).filter_by(task_id=task_id, revision=revision).all()
    ]
    if run_ids:
        modes.extend(
            a.evidence_mode
            for a in session.query(ArtifactRow)
            .filter(ArtifactRow.run_id.in_(run_ids), ArtifactRow.state == "COMMITTED")
            .all()
        )
    # 上游：本修订准备产物的证据模式（run_id 为空的准备/模板来源）
    prep = (
        session.query(PreparationRow)
        .filter_by(task_id=task_id, revision=revision)
        .order_by(PreparationRow.created_at.desc())
        .first()
    )
    if prep is not None and project_id:
        logicals = sorted((prep.prepared_artifacts or {}).keys())
        if logicals:
            modes.extend(
                a.evidence_mode
                for a in session.query(ArtifactRow)
                .filter(
                    ArtifactRow.project_id == project_id,
                    ArtifactRow.logical_path.in_(logicals),
                    ArtifactRow.state == "COMMITTED",
                )
                .all()
            )

    if not modes:
        return "UNKNOWN"
    if any(m == "MOCK" for m in modes):
        return "MOCK"
    if all(m == "REAL" for m in modes):
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
    session: Session, runs: list[RunRow], bundle_id: str, *, metric_defs: dict[str, Any] | None = None
) -> list[ClaimRow]:
    """生成确定性 Claim：DRAFT → 数字与来源校验后 CONFIRMED（模型解释只是草稿）。

    Claim 数字取**独立重算值**（verify/extract 从原始报告复算）；
    与适配器提取器产物（metrics artifact）交叉一致才 CONFIRMED——
    证明提取与算术一致性（定义书 §原始数据与派生数据分开），不证明物理模型已验证。
    本函数只查询不入库（ClaimRow 由调用方在 BundleRow 之后统一 add，避免外键乱序）。
    metric_defs 由调用方按 TaskSpec 解析的能力包版本统一加载（问题 2：
    不再写死 buffer_chamber/0.1.0）。
    """
    claims: list[ClaimRow] = []
    if metric_defs is None:
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
    prepared_issues: list[dict[str, Any]] = []
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
        for logical, expected_sha in sorted((prep.prepared_artifacts or {}).items()):
            # 问题 2 验收条件 2：准备产物按**归属 + 摘要**匹配，不能仅凭 logical_path
            # 全局 first()——同名路径可能属于其它项目或内容已被替换。
            art = (
                session.query(ArtifactRow)
                .filter_by(
                    project_id=task.project_id,
                    logical_path=logical,
                    sha256=expected_sha,
                    state="COMMITTED",
                )
                .first()
            )
            if art is not None:
                manifest.append(_entry(art, "prepared_case"))
            else:
                # 声明了准备产物却找不到 (project, logical_path, sha256) 一致的已提交
                # artifact：如实记为缺项，不静默跳过、不用同名异内容文件顶替。
                prepared_issues.append(
                    {
                        "kind": "PREPARED_ARTIFACT_UNMATCHED",
                        "logical_path": logical,
                        "expected_sha256": expected_sha,
                    }
                )
    # 3) 方法与规则版本：按 TaskSpec.method 声明的包标识 + 摘要**精确解析**后冻结
    #    （问题 2：不再写死 buffer_chamber/0.1.0；版本变化必须留下可审查差异）。
    resolved = resolve_method_package(session, rev.spec.get("method"))
    metric_defs = load_metric_definitions(resolved.capability_package_id, resolved.version)
    method_package = {
        "capability_package_id": resolved.capability_package_id,
        "version": resolved.version,
        "status": resolved.status,
        "manifest_sha256": resolved.manifest_sha256,
        "declared_sha256": resolved.declared_sha256,
        "digest_match": resolved.digest_match,
        "released": resolved.released,
    }
    pkg_root = resolved.source_dir
    for name, role in (
        ("metric-definitions.json", "method_metrics"),
        ("rules.json", "rules"),
        ("domain.json", "method_domain"),
        ("manifest.json", "method_manifest"),
    ):
        p = pkg_root / name
        if p.is_file():
            # 能力包文件原样冻结并标注 UNKNOWN 以外的实际来源模式（包本体不是 MOCK 证据）
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
    #    缺项 = 必需工况完整性缺失 + 准备产物归属/摘要未匹配（问题 2）。
    uncovered = list(completeness["missing"]) + list(prepared_issues)
    bundle_complete = completeness["complete"] and not prepared_issues
    report_completeness = {"complete": bundle_complete, "missing": uncovered}
    stage(
        f"{base}/bundle.summary{infix}.json",
        _json_bytes(
            {
                "bundle_id": bundle_id,
                "task_id": task_id,
                "revision": revision,
                "evidence_mode": mode,
                "purpose": task.purpose,
                "complete": bundle_complete,
                "incomplete_items": uncovered,
                "prepared_artifact_issues": prepared_issues,
                "method_package": method_package,
                "uncovered_scope": (
                    "全部必需工况齐备"
                    if bundle_complete
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

    claims = _draft_claims(session, runs, bundle_id, metric_defs=metric_defs)
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
        completeness=report_completeness,
        manifest=manifest,
        method_package=method_package,
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


def manifest_snapshot(
    session: Session, bundle: BundleRow
) -> tuple[list[RunRow], list[ArtifactRow], list[dict[str, Any]]]:
    """按冻结 manifest 裁剪验收输入（上游报告问题 1：冻结包是验收对象）。

    - artifacts：manifest 条目按 artifact_id 精确匹配（冻结后新 Run/新 attempt 的
      证据不会混入旧包验收）；
    - verifications：从 manifest 中 role=verification 的**冻结副本字节**解析
      （VerificationRow 无不可变守卫，DB 行冻结后可能被改；验收只认冻结时的
      conclusion/attempt_id/source_artifact_ids）；
    - runs：拥有上述冻结工件的 Run（Run 当前状态保留 DB 实时值——冻结后
      attempt 更换/状态回退本身就该阻塞旧包，属于"失效而非改写"）。
    """
    manifest_ids = {e["artifact_id"] for e in bundle.manifest}
    artifacts = (
        session.query(ArtifactRow)
        .filter(ArtifactRow.artifact_id.in_(manifest_ids))
        .all()
        if manifest_ids
        else []
    )
    by_id = {a.artifact_id: a for a in artifacts}
    missing = sorted(manifest_ids - set(by_id))
    if missing:
        raise ApiError(
            ErrorCode.CONFLICT_DIGEST,
            "冻结包 manifest 引用的 artifact 不存在（来源缺失，ACCEPT 阻塞）",
            details={"bundle_id": bundle.bundle_id, "missing_artifact_ids": missing[:20]},
        )
    verifications: list[dict[str, Any]] = []
    for entry in bundle.manifest:
        if entry["role"] != "verification":
            continue
        art = by_id.get(entry["artifact_id"])
        if art is None or not art.storage_path:
            continue  # 字节缺失由 verify_bundle_integrity 报 corrupted
        raw = Path(art.storage_path).read_text(encoding="utf-8")
        body_text = raw
        if raw.startswith("#"):  # 剥离 _json_bytes 的 MOCK banner（若有）
            body_text = raw.split("\n", 1)[1] if "\n" in raw else raw
        verifications.append(json.loads(body_text))
    run_ids_with_evidence = {a.run_id for a in artifacts if a.run_id}
    run_ids_with_evidence |= {v.get("run_id") for v in verifications if v.get("run_id")}
    runs_q = (
        session.query(RunRow)
        .filter_by(task_id=bundle.task_id, revision=bundle.revision)
        .all()
    )
    runs = [r for r in runs_q if r.run_id in run_ids_with_evidence]
    return runs, artifacts, verifications


def verify_bundle_integrity(session: Session, bundle: BundleRow) -> dict[str, list[str]]:
    """冻结包完整性校验（ACCEPT 前置；上游报告问题 1）。

    1) digest 绑定：bundle_digest 必须等于 canonical(manifest) 的重算摘要；
    2) 字节校验：每个 manifest 条目按 storage_path 重读文件，sha256/length
       与冻结值一致——文件被改一字节即阻塞。
    返回 problems dict（空 = 通过）。不抛异常，供审查门并入 problems 逐条列因。
    """
    problems: dict[str, list[str]] = {}
    recomputed = sha256_hex(canonical_dumps(bundle.manifest))
    if recomputed != bundle.bundle_digest:
        problems["bundle_digest_mismatch"] = [bundle.bundle_id]
    by_id = {
        a.artifact_id: a
        for a in session.query(ArtifactRow)
        .filter(
            ArtifactRow.artifact_id.in_([e["artifact_id"] for e in bundle.manifest])
        )
        .all()
    } if bundle.manifest else {}
    corrupted: list[str] = []
    for entry in bundle.manifest:
        art = by_id.get(entry["artifact_id"])
        if art is None or not art.storage_path:
            corrupted.append(entry["artifact_id"])
            continue
        p = Path(art.storage_path)
        if not p.is_file():
            corrupted.append(entry["artifact_id"])
            continue
        data = p.read_bytes()
        if hashlib.sha256(data).hexdigest() != entry["sha256"] or len(data) != entry["length"]:
            corrupted.append(entry["artifact_id"])
    if corrupted:
        problems["bundle_artifact_corrupted"] = sorted(set(corrupted))
    return problems


def compute_completeness_manifest(
    session: Session,
    bundle: BundleRow,
    runs: list[RunRow],
    artifacts: list[ArtifactRow],
    verifications: list[dict[str, Any]],
) -> dict[str, Any]:
    """manifest 维度的必需工况完整性（冻结集合内判定；上游报告问题 1）。

    与 compute_completeness 同口径，但 Run/attempt/artifact/verification 全部
    限定在冻结集合内：冻结后补算的新证据不能让旧包由 incomplete 变 complete。
    """
    rev = (
        session.query(TaskRevisionRow)
        .filter_by(task_id=bundle.task_id, revision=bundle.revision)
        .first()
    )
    missing: list[dict[str, Any]] = []
    if rev is None:
        return {"complete": False, "missing": [{"kind": "NO_REVISION", "revision": bundle.revision}]}
    spec = rev.spec
    runs_by_cell = {(r.variant_id, r.condition_id): r for r in runs}
    artifacts_by_run: dict[str, list[ArtifactRow]] = {}
    for a in artifacts:
        if a.run_id:
            artifacts_by_run.setdefault(a.run_id, []).append(a)
    ver_by_run = {v.get("run_id"): v for v in verifications if v.get("run_id")}
    for variant in spec["variants"]:
        for condition in spec["conditions"]:
            cell = f"{variant['variant_id']}×{condition['condition_id']}"
            run = runs_by_cell.get((variant["variant_id"], condition["condition_id"]))
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
            arts = artifacts_by_run.get(run.run_id, [])
            if not any("report" in a.logical_path for a in arts):
                missing.append({"kind": "MISSING_RAW_REPORT", "cell": cell, "run_id": run.run_id})
            if run.run_id not in ver_by_run:
                missing.append({"kind": "MISSING_VERIFICATION", "cell": cell, "run_id": run.run_id})

    # 准备产物归属+摘要绑定（问题 2 验收条件 2）：冻结的 preparation 声明的每个
    # (logical_path, sha256) 必须有对应的 prepared_case 条目。声明了却没绑定
    # （同名路径属于其它项目，或内容已被替换）→ 阻塞，不静默跳过。
    by_id = {a.artifact_id: a for a in artifacts}
    prepared_entries = {
        (e["logical_path"], e["sha256"])
        for e in bundle.manifest
        if e["role"] == "prepared_case"
    }
    prep_entry = next((e for e in bundle.manifest if e["role"] == "preparation"), None)
    if prep_entry is not None:
        declared = _declared_prepared_artifacts(by_id.get(prep_entry["artifact_id"]))
        for logical, sha in sorted(declared.items()):
            if (logical, sha) not in prepared_entries:
                missing.append(
                    {
                        "kind": "PREPARED_ARTIFACT_UNBOUND",
                        "logical_path": logical,
                        "expected_sha256": sha,
                    }
                )
    return {"complete": not missing, "missing": missing}


def _declared_prepared_artifacts(art: ArtifactRow | None) -> dict[str, str]:
    """从冻结的 preparation 产物字节读回声明的 {logical_path: sha256}。"""
    if art is None or not art.storage_path:
        return {}
    p = Path(art.storage_path)
    if not p.is_file():
        return {}
    raw = p.read_text(encoding="utf-8")
    if raw.startswith("#") and "\n" in raw:  # 剥离 _json_bytes 的 MOCK banner
        raw = raw.split("\n", 1)[1]
    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return body.get("prepared_artifacts") or {}


__all__ = [
    "build_bundle",
    "bundle_evidence_mode",
    "compute_completeness",
    "compute_completeness_manifest",
    "manifest_snapshot",
    "verify_bundle_integrity",
]
