"""Worker 领取循环（Agent E / WP-07；定义书 §作业生命周期 / §Worker协议的最低事件载荷）。

领取通道选择（二选一，此处说明）：**直接调用 dsh_sim.queue.service**（同进程）。
理由：首版拓扑中 Worker 与工程服务可同进程部署于开发/演示节点，直接调用避免
HTTP 自回环；跨机部署时替换为 POST /jobs/claim + /jobs/{id}/events（nodes.py 已实现，
协议逐字段一致），本模块业务分发逻辑不变。

事件协议最低载荷（定义书 §Worker协议的最低事件载荷）：
- STARTING：attempt_id + 工作目录摘要 + 启动意图记录 ID；
- RUNNING：process_identity + 软件 build；
- HEARTBEAT：真实阶段 stage + 日志位置 log_location + 资源状态；
- COMPLETED：exit_code + 已提交 artifact 集合；
- FAILED/CANCELLED：原因 + 退出证明/退出码。

断线语义：事件先写本地 WAL（worker/wal.py），上报**数据库提交成功之后**才标 ack；
恢复后按序重传，幂等序号保证重复重放安全（问题 3：先 commit 后 ACK）。
控制参数全部走 DSH_SIM_WORKER_* env（不写死；心跳 15s/租约 90s/失联 3 次为
queue 层既定默认，定义书 §并发断线取消与补算规则）。
"""
from __future__ import annotations

import hashlib
import itertools
import json
import math
import os
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from sqlalchemy.orm import Session

from dsh_sim.adapters.star_adapter import CollectedOutputs, ExecutionBudget, JobHandle, JobStatus, StarAdapter
from dsh_sim.canonical import canonical_dumps, prepared_digest, sha256_hex
from dsh_sim.capabilities.registry import resolve_method_package
from dsh_sim.db.models import (
    ArtifactRow,
    AuthorizationRow,
    AttemptRow,
    EventRow,
    JobRow,
    LeaseRow,
    PreparationRow,
    RunRow,
    TaskRevisionRow,
    TaskRow,
)
from dsh_sim.domain.errors import ApiError, ErrorCode
from dsh_sim.queue import service as queue_service
from dsh_sim.verify.extract import check_unit_semantics, extract_run_metrics
from dsh_sim.verify.verifier import (
    load_domain,
    load_metric_definitions,
    load_rule_set,
    persist_verification,
    verify_run,
)
from dsh_sim.worker.wal import JsonlWal

# ---------------------------------------------------------------------------
# 配置（DSH_SIM_WORKER_* env；不写死）
# ---------------------------------------------------------------------------

ENV_NODE_ID = "DSH_SIM_WORKER_NODE_ID"
ENV_WORK_DIR = "DSH_SIM_WORKER_WORK_DIR"
ENV_MAX_JOBS = "DSH_SIM_WORKER_MAX_JOBS"
ENV_MAX_POLLS = "DSH_SIM_WORKER_MAX_POLLS"
ENV_POLL_INTERVAL = "DSH_SIM_WORKER_POLL_INTERVAL_SECONDS"
ENV_ARTIFACT_ROOT = "DSH_SIM_ARTIFACT_ROOT"


@dataclass(frozen=True)
class WorkerConfig:
    node_id: str
    work_root: Path
    artifact_root: Path
    max_jobs: int | None = None  # None = 不限；测试用小值防死循环
    max_polls: int | None = None  # REAL 默认由批准的墙钟预算限时；MOCK 默认保护 32 次
    poll_interval_seconds: float | None = None  # None: REAL 1s / MOCK 0s

    def __post_init__(self) -> None:
        if self.max_polls is not None and self.max_polls < 1:
            raise ValueError("max_polls 必须为正整数或 None")
        if self.poll_interval_seconds is not None and (
            not math.isfinite(self.poll_interval_seconds)
            or not 0 <= self.poll_interval_seconds <= queue_service.HEARTBEAT_INTERVAL_SECONDS
        ):
            raise ValueError("poll_interval_seconds 必须在 0 到心跳间隔之间")

    @classmethod
    def from_env(cls, repo_root: Path | None = None) -> "WorkerConfig":
        root = repo_root or Path(__file__).resolve().parents[3]
        return cls(
            node_id=os.environ.get(ENV_NODE_ID, f"node-{uuid.uuid4().hex[:8]}"),
            work_root=Path(os.environ.get(ENV_WORK_DIR, str(root / "var" / "worker"))),
            artifact_root=Path(
                os.environ.get(ENV_ARTIFACT_ROOT, str(root / "var" / "artifacts"))
            ),
            max_jobs=(
                int(os.environ[ENV_MAX_JOBS]) if os.environ.get(ENV_MAX_JOBS) else None
            ),
            max_polls=int(os.environ[ENV_MAX_POLLS]) if os.environ.get(ENV_MAX_POLLS) else None,
            poll_interval_seconds=(
                float(os.environ[ENV_POLL_INTERVAL]) if os.environ.get(ENV_POLL_INTERVAL) else None
            ),
        )


def make_adapter_from_env() -> StarAdapter:
    """显式选择 mock / cli / openfoam；没有真实环境时不回退 MOCK。"""
    kind = os.environ.get("DSH_SIM_WORKER_ADAPTER", "mock")
    if kind == "mock":
        from dsh_sim.adapters.mock_adapter import MockStarAdapter

        return MockStarAdapter(
            behavior=os.environ.get("DSH_SIM_WORKER_MOCK_BEHAVIOR", "staged")  # type: ignore[arg-type]
        )
    if kind == "cli":
        from dsh_sim.adapters.cli_adapter import StarCliAdapter

        return StarCliAdapter()
    if kind == "openfoam":
        from dsh_sim.adapters.openfoam_adapter import OpenFoamAdapter

        required = ("DSH_SIM_OPENFOAM_TEMPLATE_REGISTRY", "DSH_SIM_OPENFOAM_TEMPLATE_ROOT", ENV_WORK_DIR)
        missing = [name for name in required if not os.environ.get(name)]
        if missing:
            raise ValueError(f"OpenFOAM 需要显式模板注册与受控目录配置: {', '.join(missing)}")
        return OpenFoamAdapter()
    raise ValueError(f"未知 DSH_SIM_WORKER_ADAPTER: {kind!r}（只允许 mock/cli/openfoam）")


# ---------------------------------------------------------------------------
# 事件上报（WAL 先行）
# ---------------------------------------------------------------------------


class _EventChannel:
    def __init__(self, session: Session, wal: JsonlWal, job: JobRow, lease: LeaseRow) -> None:
        self._session = session
        self._wal = wal
        self.job = job
        self.lease = lease
        # 事务失败会 expire/detach ORM 对象；WAL 身份不能依赖随后懒加载。
        self._job_id = job.job_id
        self._lease_id = lease.lease_id
        self._fencing_token = lease.fencing_token
        last = (
            session.query(EventRow.event_seq)
            .filter(EventRow.job_id == job.job_id)
            .order_by(EventRow.event_seq.desc())
            .first()
        )
        self._seq = itertools.count((last[0] if last else 0) + 1)

    def post(self, kind: str, payload: dict[str, Any]) -> int:
        seq = next(self._seq)
        self._wal.append(
            job_id=self._job_id,
            lease_id=self._lease_id,
            fencing_token=self._fencing_token,
            event_seq=seq,
            kind=kind,
            payload=payload,
        )
        queue_service.post_event(
            self._session,
            job_id=self._job_id,
            lease_id=self._lease_id,
            fencing_token=self._fencing_token,
            event_seq=seq,
            kind=kind,
            payload=payload,
        )
        self._session.commit()  # 事件增量持久化：断线后重进可见相同状态（FR-11）
        self._wal.mark_acked(self._job_id, seq)
        return seq


def replay_wal(session: Session, wal: JsonlWal) -> int:
    """恢复后按序重传未确认事件；租约已失效的记录不再重传（Job 已转 LOST 待核实）。

    上游验收报告问题 3（2026-09-22）：顺序必须是 **DB commit → 本地 mark_acked**。
    旧实现逐项先 mark_acked、循环外才统一 commit：提交失败或进程在窗口内崩溃时
    本地认为已上报、数据库却没有该事件，而该记录已标记 acked 不会再重放——
    事件永久丢失。现在每条记录走独立的 post → commit → ack：

    - 提交失败（或崩溃在 ack 之前）：记录保持未 ACK 原样留在 WAL，下次重放；
    - 提交成功后才确认本地 ACK；提交后/ACK 前崩溃 → 下次重放同一事件，
      服务端 (job_id,event_seq) 唯一约束 + 幂等返回使重复重放不产生重复事件、
      也不制造非法状态迁移（终态事件不覆盖已撤销，非法中间态按原样跳过）；
    - 租约失效 / 序号空洞 / 状态冲突：保留待人工核查，不静默丢弃。
    """
    sent = 0
    for rec in wal.unacked():
        lease = session.get(LeaseRow, rec["lease_id"])
        if lease is None or not lease.active:
            continue  # 旧租约事件永不会被接受（fencing），保留在 WAL 供人工核查
        try:
            queue_service.post_event(
                session,
                job_id=rec["job_id"],
                lease_id=rec["lease_id"],
                fencing_token=rec["fencing_token"],
                event_seq=rec["event_seq"],
                kind=rec["kind"],
                payload=rec["payload"],
            )
            session.commit()  # ① 先确认数据库提交（失败则不 ACK，记录可重放）
        except ApiError:
            # post_event 的 ApiError 全部发生在 session.add(event) 之前（kind 校验、
            # job/租约/fencing/序号/状态校验），没有待丢弃的写入，因此**不调用
            # rollback**：Session.rollback() 会让事务提前失活，而 SQLite 连接级
            # BEGIN IMMEDIATE 的保留锁此时不会落到 DBAPI，要等下一次真实 commit 才
            # 释放（db/session.py）。留着事务让后续 commit 正常收尾，锁才不滞留。
            continue  # 序号空洞/状态冲突：保留待核查，不静默丢弃
        except Exception:
            # 提交失败：丢弃挂起写入并**丢弃底层连接**。SQLite 下 rollback 不会释放
            # BEGIN IMMEDIATE 的保留锁（db/session.py 的 begin 事件绕过了 DBAPI 事务
            # 跟踪），连接带着锁归还连接池会阻塞其它连接；invalidate() 关闭连接才
            # 真正放锁。记录保持未 ACK，留在 WAL 供下次重放。
            session.invalidate()
            raise
        wal.mark_acked(rec["job_id"], rec["event_seq"])  # ② 提交成功后才确认本地 ACK
        sent += 1
    # 收尾：结束租约查找开启的事务（SQLite 下 begin 事件把所有事务提升为
    # BEGIN IMMEDIATE，不释放会阻塞其它连接；无待提交写入时提交为空操作）。
    # 全部记录都走 ApiError 分支时，这一步同时负责释放保留锁。
    session.commit()
    return sent


# ---------------------------------------------------------------------------
# artifact 落库（提交=真实文件+长度+摘要核对，定义书 §上传与大文件）
# ---------------------------------------------------------------------------


def _commit_artifact(
    session: Session,
    *,
    project_id: str,
    job_id: str,
    logical_path: str,
    src_path: Path,
    artifact_root: Path,
    evidence_mode: str,
    run_id: str | None = None,
    attempt_id: str | None = None,
    expected_sha256: str | None = None,
) -> ArtifactRow:
    content = src_path.read_bytes()
    sha = hashlib.sha256(content).hexdigest()
    if expected_sha256 is not None and sha != expected_sha256:
        raise ValueError(f"产物摘要核验失败: {logical_path}")
    artifact_id = f"art_{uuid.uuid4().hex[:24]}"
    dest_dir = artifact_root / project_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / artifact_id
    tmp = dest_dir / f".{artifact_id}.tmp"
    tmp.write_bytes(content)
    os.replace(tmp, dest)  # 临时区→原子重命名
    row = ArtifactRow(
        artifact_id=artifact_id,
        project_id=project_id,
        logical_path=logical_path,
        length=len(content),
        sha256=sha,
        state="COMMITTED",
        job_id=job_id,
        run_id=run_id,
        attempt_id=attempt_id,
        evidence_mode=evidence_mode,
        storage_path=str(dest),
    )
    session.add(row)
    session.flush()
    return row


# ---------------------------------------------------------------------------
# PREPARE 作业
# ---------------------------------------------------------------------------


def _whitelist_writes(condition: dict[str, Any]) -> list:
    from dsh_sim.adapters.star_adapter import WhitelistWrite

    writes = []
    for f in condition["fields"]:
        q = f["quantity"]
        writes.append(
            WhitelistWrite(
                field_id=f["field"],
                boundary_role=f["role_id"],
                value_si=float(q["si_value"]),
                unit=q["unit"],
                source_ref=q.get("source_ref", ""),
            )
        )
    return writes


def _latest_preparation(session: Session, task_id: str, revision: int) -> PreparationRow:
    prep = (
        session.query(PreparationRow)
        .filter_by(task_id=task_id, revision=revision)
        .order_by(PreparationRow.created_at.desc())
        .first()
    )
    if prep is None:
        raise ApiError(
            code=__import__("dsh_sim.domain.errors", fromlist=["ErrorCode"]).ErrorCode.VALIDATION,
            message="PREPARE 作业找不到对应 Preparation 记录",
            details={"task_id": task_id, "revision": revision},
        )
    return prep


def _do_prepare(
    session: Session,
    job: JobRow,
    channel: _EventChannel,
    adapter: StarAdapter,
    config: WorkerConfig,
) -> None:
    task = session.get(TaskRow, job.task_id)
    rev = (
        session.query(TaskRevisionRow)
        .filter_by(task_id=task.task_id, revision=task.current_revision)
        .first()
    )
    spec = rev.spec
    prep = _latest_preparation(session, task.task_id, task.current_revision)
    channel.post(
        "STARTING",
        {
            "preparation_id": prep.preparation_id,
            "revision": prep.revision,
            "launch_intent_id": f"intent-{job.job_id}",
            "work_dir_summary": sha256_hex(str(config.work_root / task.task_id))[:16],
            "stage": "environment_probe",
        },
    )
    # 外部调用期间不持 SQLite BEGIN IMMEDIATE 锁，取消与查看进度应可并行。
    session.commit()
    probe = adapter.probe_environment()
    if probe.evidence_mode not in {"MOCK", "REAL"}:
        raise ValueError("适配器未声明有效 evidence_mode")
    if probe.evidence_mode == "REAL":
        _resolve_execution_method(session, spec, probe.evidence_mode)
    channel.post(
        "RUNNING",
        {
            "process_identity": {"worker_node_id": config.node_id, "operation": "prepare"},
            "software_build": probe.star_build,
            "stage": "prepare_and_readback",
            "evidence_mode": probe.evidence_mode,
        },
    )

    # 单位与压力语义检查（FR-04）：表压无参考绝压等 → 差异/阻塞，不进入 READY
    semantic_findings = check_unit_semantics(spec)

    prepared_artifacts: dict[str, str] = {}
    differences: list[dict[str, Any]] = []
    readback_shas: list[str] = []
    artifact_ids: list[str] = []

    for variant in spec["variants"]:
        for condition in spec["conditions"]:
            cell_dir = (
                config.work_root
                / task.task_id
                / "prepare"
                / f"revision-{prep.revision}"
                / prep.preparation_id
                / variant["variant_id"]
                / condition["condition_id"]
            )
            session.commit()
            if probe.evidence_mode == "REAL":
                inspection = adapter.inspect_template(variant["template_artifact_id"])
                if (inspection.evidence_mode != "REAL"
                    or inspection.mesh_summary.get("boundary_map_sha256") != variant["boundary_map_sha256"]):
                    raise ValueError("REAL 模板的边界映射摘要缺失或与 TaskSpec 不一致")
            case = adapter.prepare_case(
                template_ref=variant["template_artifact_id"],
                whitelist_writes=_whitelist_writes(condition),
                work_dir=str(cell_dir),
            )
            readback = adapter.read_actual_settings(case.prepared_sim_path)
            if case.evidence_mode != probe.evidence_mode or readback.evidence_mode != probe.evidence_mode:
                raise ValueError("准备/回读 evidence_mode 与探针不一致")
            if probe.evidence_mode == "REAL" and case.source_template_sha256 != variant["template_sha256"]:
                raise ValueError("真实模板摘要与 TaskSpec 不一致，不使用其它模板顶替")
            readback_shas.append(readback.readback_sha)

            logical = (
                f"prepared/{task.task_id}/revision-{prep.revision}/{prep.preparation_id}/"
                f"{variant['variant_id']}/{condition['condition_id']}/"
                + Path(case.prepared_sim_path).name
            )
            art = _commit_artifact(
                session,
                project_id=task.project_id,
                job_id=job.job_id,
                logical_path=logical,
                src_path=Path(case.prepared_sim_path),
                artifact_root=config.artifact_root,
                evidence_mode=case.evidence_mode,
                expected_sha256=case.prepared_sha256,
            )
            artifact_ids.append(art.artifact_id)
            prepared_artifacts[logical] = case.prepared_sha256

            for e in readback.entries:
                if e.status != "MATCH":
                    differences.append(
                        {
                            "field": e.field_id,
                            "boundary": next(
                                (w.boundary_role for w in case.writes_applied if w.field_id == e.field_id),
                                "",
                            ),
                            "requested": e.requested_value_si,
                            "readback": e.actual_value_si,
                            "unit": next(
                                (w.unit for w in case.writes_applied if w.field_id == e.field_id),
                                "",
                            ),
                            "status": e.status,
                        }
                    )

    # 语义 finding 也作为差异阻塞（不静默跳过）
    for f_ in semantic_findings:
        differences.append({"field": f_["kind"], "status": "MISMATCH", "requested": None, "readback": None, "unit": "", "boundary": "", "message": f_["message"]})

    combined_readback_sha = sha256_hex(canonical_dumps(readback_shas))
    from dsh_sim.api.services.prep_service import mark_preparation_ready

    session.refresh(task)
    if task.current_revision != prep.revision:
        raise ValueError("准备过程中任务已换修订，旧准备不标 READY")
    session.refresh(job)
    if job.cancel_requested:
        session.commit()
        channel.post("CANCELLED", {
            "reason": "准备操作返回后确认取消，未启动 EXECUTE 求解进程",
            "exit_proof": {"process_exited": True, "process_group_exited": True,
                           "evidence": "prepare/readback returned; EXECUTE not launched"},
            "exit_code": 130, "evidence_mode": probe.evidence_mode,
            "artifact_ids": artifact_ids,
        })
        return
    mark_preparation_ready(
        session,
        prep.preparation_id,
        prepared_artifacts=prepared_artifacts,
        readback_sha256=combined_readback_sha,
        adapter_build=getattr(adapter, "adapter_build", "unknown"),
        software_build=probe.star_build,
        differences=differences,
    )
    if differences:
        prep.blockers = [
            {
                "code": "READBACK_DIFFERENCE",
                "message": f"存在 {len(differences)} 项申请/回读差异或单位语义问题，未清除前不能授权",
                "responsible": task.owner_id,
                "action": "核对差异并创建新修订或重新准备",
            }
        ]
    session.commit()

    channel.post(
        "COMPLETED",
        {
            "exit_code": 0,
            "artifact_ids": artifact_ids,
            "preparation_id": prep.preparation_id,
            "ready": not differences,
            "evidence_mode": probe.evidence_mode,
        },
    )


# ---------------------------------------------------------------------------
# EXECUTE 作业
# ---------------------------------------------------------------------------


def _resolve_execution_method(session: Session, spec: dict[str, Any], mode: str):
    """REAL 输入精确绑定磁盘方法包；旧 Mock 夹具的假摘要必须显式留痕。"""
    resolved = resolve_method_package(session, spec.get("method"))
    exact = resolved.digest_match and resolved.manifest_sha256 == resolved.declared_sha256
    if mode == "REAL" and not exact:
        raise ValueError("REAL 方法包摘要与 TaskSpec 不一致，停止执行；不回退当前版本")
    return resolved


def _find_prepared_artifact(
    session: Session, prep: PreparationRow | None, variant_id: str, condition_id: str,
) -> ArtifactRow | None:
    """准备引用同时绑定任务、修订、准备作业、逻辑路径、摘要及真实文件。"""
    if prep is None:
        return None
    task = session.get(TaskRow, prep.task_id)
    rev = session.query(TaskRevisionRow).filter_by(task_id=prep.task_id, revision=prep.revision).first()
    if task is None or rev is None or not prep.ready:
        return None
    digest = prepared_digest(rev.spec_sha256, prep.prepared_artifacts, prep.readback_sha256, prep.adapter_build)
    if digest != prep.prepared_digest:
        raise ValueError("准备摘要不符，拒绝替换或损坏的准备记录")
    matches = []
    for logical, expected_sha in prep.prepared_artifacts.items():
        parts = PurePosixPath(logical).parts
        if len(parts) < 3 or parts[-3:-1] != (variant_id, condition_id):
            continue
        candidates = (session.query(ArtifactRow).join(JobRow, ArtifactRow.job_id == JobRow.job_id)
            .filter(ArtifactRow.project_id == task.project_id, ArtifactRow.logical_path == logical,
                    ArtifactRow.sha256 == expected_sha, ArtifactRow.state == "COMMITTED",
                    JobRow.task_id == prep.task_id, JobRow.kind == "PREPARE").all())
        for art in candidates:
            starts = session.query(EventRow).filter_by(job_id=art.job_id, kind="STARTING").all()
            if not any((e.payload or {}).get("preparation_id") == prep.preparation_id for e in starts):
                continue
            path = Path(art.storage_path) if art.storage_path else None
            if path is None or not path.is_file():
                continue
            content = path.read_bytes()
            if len(content) != art.length or hashlib.sha256(content).hexdigest() != expected_sha:
                raise ValueError("准备文件长度/摘要损坏，拒绝启动")
            matches.append(art)
    if len(matches) > 1:
        raise ValueError("工况匹配多个准备 artifact，拒绝不明确的输入绑定")
    return matches[0] if matches else None


def _find_prepared_path(
    session: Session, prep: PreparationRow | None, variant_id: str, condition_id: str,
) -> str | None:
    art = _find_prepared_artifact(session, prep, variant_id, condition_id)
    return art.storage_path if art is not None else None


def _output_path(work_dir: Path, logical: str, collected: CollectedOutputs) -> Path:
    relative = PurePosixPath(logical)
    if relative.is_absolute() or ".." in relative.parts or "\\" in logical:
        raise ValueError(f"产物逻辑路径不受控: {logical}")
    root = work_dir.resolve()
    candidates = [root / logical]
    declared = [*collected.raw_reports, *collected.monitors_csv, *collected.scenes]
    if collected.result_sim:
        declared.append(collected.result_sim)
    candidates.extend(Path(p) for p in declared if Path(p).name == relative.name)
    candidates.append(root / relative.name)  # 原有 STAR Mock 的 raw/name -> work/name 约定
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved.is_relative_to(root) and resolved.is_file():
            return resolved
    raise ValueError(f"产物未在本 attempt 工作目录内找到: {logical}")


def _register_collected(
    session: Session, config: WorkerConfig, collected: CollectedOutputs, handle: JobHandle,
    *, project_id: str, job_id: str, run_id: str, attempt_id: str, mode: str,
) -> tuple[list[str], ArtifactRow | None, ArtifactRow | None]:
    if collected.run_id != run_id or collected.attempt_no != handle.attempt_no:
        raise ValueError("收集产物 run/attempt 与启动句柄不一致")
    if collected.evidence_mode != mode:
        raise ValueError("产物 evidence_mode 与运行模式不一致")
    report_paths = {Path(p).resolve() for p in collected.raw_reports}
    monitor_paths = {Path(p).resolve() for p in collected.monitors_csv}
    ids: list[str] = []
    report_art = monitor_art = None
    for logical, expected in collected.artifact_digests.items():
        local = _output_path(Path(handle.work_dir), logical, collected)
        full_logical = f"runs/{run_id}/attempt-{handle.attempt_no}/{logical}"
        if hashlib.sha256(local.read_bytes()).hexdigest() != expected:
            raise ValueError(f"产物摘要核验失败: {logical}")
        existing = session.query(ArtifactRow).filter_by(
            project_id=project_id, job_id=job_id, run_id=run_id, attempt_id=attempt_id,
            logical_path=full_logical, sha256=expected, state="COMMITTED",
        ).all()
        if len(existing) > 1:
            raise ValueError(f"产物登记存在歧义: {logical}")
        if existing:
            art = existing[0]
            frozen = Path(art.storage_path).read_bytes() if art.storage_path else b""
            if len(frozen) != art.length or hashlib.sha256(frozen).hexdigest() != expected or art.evidence_mode != mode:
                raise ValueError(f"已冻结产物损坏或模式不符: {logical}")
        else:
            art = _commit_artifact(
                session, project_id=project_id, job_id=job_id, logical_path=full_logical,
                src_path=local, artifact_root=config.artifact_root, evidence_mode=mode,
                run_id=run_id, attempt_id=attempt_id, expected_sha256=expected,
            )
        session.commit()  # 已取得的证据逐项保留，后续解析失败不能删除原始证据
        ids.append(art.artifact_id)
        if local in report_paths and local.name in {"report.csv", "report.mock.csv"}:
            if report_art is not None:
                raise ValueError("当前 attempt 存在多个规范报告 CSV")
            report_art = art
        if local in monitor_paths and local.name in {"monitor.csv", "monitor.mock.csv"}:
            if monitor_art is not None:
                raise ValueError("当前 attempt 存在多个规范监控 CSV")
            monitor_art = art
    return ids, report_art, monitor_art


def _exit_confirmed(status: JobStatus, handle: JobHandle, mode: str) -> bool:
    if status.job_id != handle.job_id or status.phase not in {"SUCCEEDED", "FAILED", "CANCELLED"}:
        return False
    proof = status.detail.get("exit_proof")
    if isinstance(proof, dict):
        return (proof.get("process_exited") is True and proof.get("process_group_exited") is True
                and (mode == "MOCK" or proof.get("process_identity") == handle.process_identity))
    # 原有内存 Mock 协议的文字证明只在显式 MOCK 下兼容；REAL 不接受一句自然语言。
    return (mode == "MOCK" and status.detail.get("evidence_mode") == "MOCK"
            and isinstance(proof, str) and proof.lower().startswith("mock"))


def _retain_failure_outputs(
    session: Session, config: WorkerConfig, adapter: StarAdapter, handle: JobHandle | None,
    work_dir: Path, *, project_id: str, job_id: str, run_id: str, attempt_id: str,
    attempt_no: int, mode: str,
) -> tuple[list[str], list[str]]:
    ids: list[str] = []
    errors: list[str] = []
    session.commit()
    if handle is not None:
        try:
            collected = adapter.collect_outputs(handle)
            ids, _, _ = _register_collected(
                session, config, collected, handle, project_id=project_id, job_id=job_id,
                run_id=run_id, attempt_id=attempt_id, mode=mode,
            )
        except Exception as exc:
            errors.append(f"collect_outputs: {type(exc).__name__}: {exc}")
            session.rollback()
            session.invalidate()  # 回收 SQLite 异常事务可能仍持有的保留锁
    # 无完整清单时仍冻结现场已有日志/监督记录；这些是有时间边界的部分快照。
    if errors or not ids:
        root = work_dir.resolve()
        for path in sorted(root.rglob("*")) if root.exists() else []:
            if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(root):
                continue
            if path.suffix not in {".log", ".json"}:
                continue
            try:
                art = _commit_artifact(
                    session, project_id=project_id, job_id=job_id,
                    logical_path=f"runs/{run_id}/attempt-{attempt_no}/failure-snapshot/{path.relative_to(root).as_posix()}",
                    src_path=path, artifact_root=config.artifact_root, evidence_mode=mode,
                    run_id=run_id, attempt_id=attempt_id,
                )
                session.commit()
                ids.append(art.artifact_id)
            except Exception as exc:
                errors.append(f"snapshot {path.name}: {type(exc).__name__}: {exc}")
                session.rollback()
                session.invalidate()
    ids = sorted(set(ids) | {row.artifact_id for row in session.query(ArtifactRow).filter_by(
        project_id=project_id, run_id=run_id, attempt_id=attempt_id, state="COMMITTED")})
    session.commit()
    return ids, errors


def _finish_interrupted(
    session: Session, channel: _EventChannel, config: WorkerConfig, adapter: StarAdapter,
    handle: JobHandle | None, work_dir: Path, *, project_id: str, job_id: str,
    run_id: str, attempt_id: str, attempt_no: int, mode: str, reason: str,
    exit_code: int, requested_outcome: str = "FAILED", launch_attempted: bool = False,
) -> None:
    session.commit()  # adapter.cancel/collect 均不能跨阻塞操作持有数据库锁
    stopped = handle is None and not launch_attempted
    proof: Any = {"process_exited": True, "process_group_exited": True,
                  "evidence": "launch was not invoked"} if stopped else None
    cancellation_error = None
    if handle is not None:
        try:
            status = adapter.cancel(handle)
            stopped = _exit_confirmed(status, handle, mode)
            proof = status.detail.get("exit_proof")
            if not stopped:
                cancellation_error = f"取消返回 {status.phase}，没有可核验的完整退出证据"
        except Exception as exc:
            cancellation_error = f"cancel: {type(exc).__name__}: {exc}"
    ids, errors = _retain_failure_outputs(
        session, config, adapter, handle, work_dir, project_id=project_id, job_id=job_id,
        run_id=run_id, attempt_id=attempt_id, attempt_no=attempt_no, mode=mode,
    )
    payload = {
        "reason": reason, "exit_code": exit_code, "exit_proof": proof,
        "exit_unconfirmed": not stopped, "execution_state": requested_outcome if stopped else "LOST",
        "evidence_mode": mode, "artifact_ids": ids, "partial_outputs": True,
        "collection_errors": errors,
    }
    if cancellation_error:
        payload["cancellation_error"] = cancellation_error
    if handle is not None:
        payload["process_identity"] = handle.process_identity
    channel.post(requested_outcome if stopped else "FAILED", payload)


def _do_execute(
    session: Session, job: JobRow, channel: _EventChannel, adapter: StarAdapter, config: WorkerConfig,
) -> None:
    run = session.get(RunRow, job.run_id)
    attempt = session.get(AttemptRow, job.attempt_id)
    task = session.get(TaskRow, run.task_id)
    rev = session.query(TaskRevisionRow).filter_by(task_id=task.task_id, revision=run.revision).one()
    spec = rev.spec
    # 缓存标识，外部操作前结束事务，异常失效连接后也能保存现场。
    job_id, run_id, attempt_id, attempt_no = job.job_id, run.run_id, attempt.attempt_id, attempt.attempt_no
    project_id = task.project_id
    work_dir = config.work_root / run_id / f"attempt-{attempt_no}"
    work_dir.mkdir(parents=True, exist_ok=True)
    mode = "UNKNOWN"
    handle = None
    launch_attempted = False
    channel.post("STARTING", {
        "attempt_id": attempt_id, "launch_intent_id": f"intent-{job_id}",
        "work_dir_summary": sha256_hex(str(work_dir))[:16], "stage": "environment_probe",
    })
    try:
        session.commit()
        probe = adapter.probe_environment()
        mode = probe.evidence_mode
        if mode not in {"MOCK", "REAL"}:
            raise ValueError("适配器未声明有效 evidence_mode")
        resolved = _resolve_execution_method(session, spec, mode)
        auth = (session.query(AuthorizationRow)
                .filter_by(task_id=task.task_id, revision=run.revision, validity="CURRENT", revoked_at=None)
                .order_by(AuthorizationRow.created_at.desc()).first())
        if auth is None:
            raise ValueError("找不到本修订仍有效的人工授权，不使用 spec 预算替代授权")
        prep = session.get(PreparationRow, auth.preparation_id)
        if prep is None or prep.task_id != task.task_id or prep.revision != run.revision or prep.prepared_digest != auth.prepared_digest:
            raise ValueError("授权与准备任务/修订/摘要不一致")
        art = _find_prepared_artifact(session, prep, run.variant_id, run.condition_id)
        if art is None:
            raise ValueError("prepared 产物缺失或未绑定本任务/修订/准备，拒绝同名替代")
        if mode == "REAL" and art.evidence_mode != "REAL":
            raise ValueError("REAL 执行不能使用 MOCK/UNKNOWN 准备产物")
        staged = work_dir / PurePosixPath(art.logical_path).name
        content = Path(art.storage_path).read_bytes()
        if hashlib.sha256(content).hexdigest() != art.sha256:
            raise ValueError("准备产物 staging 前摘要改变")
        staged.write_bytes(content)
        raw_budget = auth.execution_budget or {}
        wall_seconds = float(raw_budget.get("wallclock_hours", 0)) * 3600
        cpu_cores = int(raw_budget.get("cpu_cores", 0))
        if not math.isfinite(wall_seconds) or wall_seconds <= 0 or cpu_cores < 1:
            raise ValueError("授权预算缺失或无效")
        budget = ExecutionBudget(max_iterations=None, wall_clock_seconds=math.ceil(wall_seconds),
                                 cpu_cores=cpu_cores, attempt_no=attempt_no)
        method_exact = resolved.digest_match and resolved.manifest_sha256 == resolved.declared_sha256
        launch_record = {
            "job_id": job_id, "run_id": run_id, "attempt_id": attempt_id,
            "preparation_id": prep.preparation_id, "prepared_digest": prep.prepared_digest,
            "prepared_ref_sha": art.sha256, "adapter_build": getattr(adapter, "adapter_build", "unknown"),
            "evidence_mode": mode, "method_version": resolved.version,
            "method_digest_match": method_exact, "mock_method_fixture": mode == "MOCK" and not method_exact,
            "budget": asdict(budget),
            "environment_probe": asdict(probe),
        }
        (work_dir / "worker-launch-intent.json").write_text(json.dumps(launch_record, ensure_ascii=False, indent=2), encoding="utf-8")
        session.refresh(job)
        if job.cancel_requested:
            _finish_interrupted(session, channel, config, adapter, None, work_dir,
                project_id=project_id, job_id=job_id, run_id=run_id, attempt_id=attempt_id,
                attempt_no=attempt_no, mode=mode, reason="启动前收到取消，未调用 launch",
                exit_code=130, requested_outcome="CANCELLED")
            return
        session.commit()
        deadline = time.monotonic() + wall_seconds
        launch_attempted = True
        handle = adapter.launch(str(staged), budget)
        if handle.run_id != run_id or handle.attempt_no != attempt_no or Path(handle.work_dir).resolve() != work_dir.resolve():
            raise ValueError("启动句柄与本 run/attempt/工作目录不一致")
        (work_dir / "worker-job-handle.json").write_text(json.dumps(asdict(handle), ensure_ascii=False, indent=2), encoding="utf-8")
        channel.post("RUNNING", {
            "process_identity": {"identity": handle.process_identity, "adapter_job_id": handle.job_id,
                                 "work_dir": handle.work_dir, "launch_args": list(handle.launch_args)},
            "software_build": probe.star_build, "stage": "launch", "evidence_mode": mode,
            "method_version": resolved.version, "method_digest_match": method_exact,
            "mock_method_fixture": mode == "MOCK" and not method_exact,
        })
        interval = config.poll_interval_seconds
        if interval is None:
            interval = 0.0 if mode == "MOCK" else 1.0
        poll_limit = config.max_polls if config.max_polls is not None else (32 if mode == "MOCK" else None)
        polls = 0
        while True:
            session.refresh(job)
            cancel_requested = job.cancel_requested
            session.commit()  # 查询取消标志会开启 BEGIN IMMEDIATE，等待前必须提交
            if cancel_requested:
                _finish_interrupted(session, channel, config, adapter, handle, work_dir,
                    project_id=project_id, job_id=job_id, run_id=run_id, attempt_id=attempt_id,
                    attempt_no=attempt_no, mode=mode, reason="用户取消请求，检查受控进程组退出",
                    exit_code=130, requested_outcome="CANCELLED", launch_attempted=True)
                return
            if time.monotonic() >= deadline or (poll_limit is not None and polls >= poll_limit):
                reason = "批准的墙钟预算耗尽" if time.monotonic() >= deadline else f"poll 超过保护上限 {poll_limit}"
                _finish_interrupted(session, channel, config, adapter, handle, work_dir,
                    project_id=project_id, job_id=job_id, run_id=run_id, attempt_id=attempt_id,
                    attempt_no=attempt_no, mode=mode, reason=reason, exit_code=124, launch_attempted=True)
                return
            status = adapter.poll(handle)
            polls += 1
            if status.job_id != handle.job_id:
                raise ValueError("poll 返回其它作业状态")
            if status.phase in {"FAILED", "CANCELLED", "LOST"}:
                _finish_interrupted(session, channel, config, adapter, handle, work_dir,
                    project_id=project_id, job_id=job_id, run_id=run_id, attempt_id=attempt_id,
                    attempt_no=attempt_no, mode=mode, reason=f"适配器报告 {status.phase}",
                    exit_code=status.detail.get("exit_code") or 2,
                    requested_outcome="CANCELLED" if status.phase == "CANCELLED" else "FAILED",
                    launch_attempted=True)
                return
            if status.phase == "SUCCEEDED":
                if mode == "REAL":
                    if not _exit_confirmed(status, handle, mode):
                        raise ValueError("REAL SUCCEEDED 缺少完整进程组退出证明")
                    code = status.detail.get("exit_code")
                    if type(code) is not int or code != 0 or status.detail["exit_proof"].get("returncode") != 0:
                        raise ValueError("REAL SUCCEEDED 与已观测退出码不一致")
                break
            if status.phase not in {"STARTING", "RUNNING", "COLLECTING", "CANCELLING"}:
                raise ValueError(f"未知适配器阶段: {status.phase}")
            channel.post("HEARTBEAT", {
                "stage": status.phase, "log_location": status.log_location,
                "resource_state": {"cpu_cores": budget.cpu_cores}, "evidence_mode": mode,
            })
            session.commit()
            time.sleep(min(interval, max(0.0, deadline - time.monotonic())))

        session.commit()
        collected = adapter.collect_outputs(handle)
        artifact_ids, report_art, monitor_art = _register_collected(
            session, config, collected, handle, project_id=project_id, job_id=job_id,
            run_id=run_id, attempt_id=attempt_id, mode=mode,
        )
        raw_metrics = adapter.extract_metrics(collected)
        if raw_metrics.evidence_mode != mode:
            raise ValueError("指标 evidence_mode 与原始运行不一致")
        infix = ".mock" if mode == "MOCK" else ""
        metrics_path = work_dir / f"metrics{infix}.json"
        body = {"evidence_mode": mode, "extractor_id": raw_metrics.extractor_id,
                "extractor_version": raw_metrics.extractor_version,
                "metric_values": raw_metrics.metric_values, "missing_metrics": list(raw_metrics.missing_metrics),
                "source_artifacts": raw_metrics.source_artifacts}
        banner = "# MOCK DATA - NOT REAL SOLVER OUTPUT\n" if mode == "MOCK" else ""
        metrics_path.write_text(banner + json.dumps(body, ensure_ascii=False, indent=1, allow_nan=False), encoding="utf-8")
        metrics_art = _commit_artifact(session, project_id=project_id, job_id=job_id,
            logical_path=f"runs/{run_id}/attempt-{attempt_no}/metrics{infix}.json", src_path=metrics_path,
            artifact_root=config.artifact_root, evidence_mode=mode, run_id=run_id, attempt_id=attempt_id)
        session.commit()
        artifact_ids.append(metrics_art.artifact_id)
        # 执行结束再次解析，防止执行期间包文件变化后悄悄用新口径校核。
        resolved = _resolve_execution_method(session, spec, mode)
        pkg_id, pkg_ver = resolved.capability_package_id, resolved.version
        rules, rule_set_sha = load_rule_set(pkg_id, pkg_ver)
        metric_defs = load_metric_definitions(pkg_id, pkg_ver)
        domain = load_domain(pkg_id, pkg_ver)
        extracted = extract_run_metrics(report_art.storage_path if report_art else None,
            monitor_art.storage_path if monitor_art else None, metric_definitions=metric_defs)
        extracted.findings.extend(check_unit_semantics(spec))
        result = verify_run(extracted=extracted, rules=rules, domain=domain,
                            required_metrics=(spec.get("method") or {}).get("required_metrics", []))
        run = session.get(RunRow, run_id)
        ver = persist_verification(session, run=run, attempt_id=attempt_id, result=result,
            rule_set_sha256=rule_set_sha,
            source_artifact_ids=[a.artifact_id for a in (report_art, monitor_art, metrics_art) if a is not None])
        session.commit()
        session.refresh(job)
        cancelled_during_collection = job.cancel_requested
        session.commit()
        if cancelled_during_collection:
            _finish_interrupted(session, channel, config, adapter, handle, work_dir,
                project_id=project_id, job_id=job_id, run_id=run_id, attempt_id=attempt_id,
                attempt_no=attempt_no, mode=mode, reason="收集期间收到取消，保留已收集证据",
                exit_code=130, requested_outcome="CANCELLED", launch_attempted=True)
            return
        channel.post("COMPLETED", {"exit_code": 0, "exit_proof": status.detail.get("exit_proof"),
            "artifact_ids": artifact_ids, "verification_id": ver.verification_id,
            "numerical_conclusion": result.conclusion, "applicability": result.applicability,
            "evidence_mode": mode, "method_version": resolved.version,
            "method_digest_match": resolved.digest_match})
    except BaseException as exc:
        session.rollback()
        session.invalidate()
        try:
            _finish_interrupted(session, channel, config, adapter, handle, work_dir,
                project_id=project_id, job_id=job_id, run_id=run_id, attempt_id=attempt_id,
                attempt_no=attempt_no, mode=mode, reason=f"Worker 执行异常: {type(exc).__name__}: {exc}",
                exit_code=130 if isinstance(exc, KeyboardInterrupt) else 1,
                requested_outcome="CANCELLED" if isinstance(exc, KeyboardInterrupt) else "FAILED",
                launch_attempted=launch_attempted)
        finally:
            # Ctrl-C/SystemExit 先清理受控进程、冻结证据，再交还调用方；不伪装正常完成。
            if not isinstance(exc, Exception):
                raise exc


# ---------------------------------------------------------------------------
# 领取循环入口
# ---------------------------------------------------------------------------


def run_until_idle(
    session: Session,
    adapter: StarAdapter,
    config: WorkerConfig,
    *,
    should_stop: Callable[[], bool] | None = None,
) -> list[str]:
    """顺序领取并处理作业直到队列空。返回处理的 job_id 列表（测试可断言）。

    should_stop 在**领取边界**检查（每次 claim 之前）：停止请求不影响正在执行的
    作业（当前作业完整跑完并落证据），只保证队列中剩余作业保持未领取。
    常驻服务（worker/service.py）用它实现"完成当前项后退出，后续项不动"。
    """
    wal = JsonlWal(config.work_root / "worker.wal.jsonl")
    replay_wal(session, wal)
    processed: list[str] = []
    count = 0
    while True:
        if should_stop is not None and should_stop():
            break  # 领取边界停止：剩余作业保持未领取，由调用方决定后续
        if config.max_jobs is not None and count >= config.max_jobs:
            break
        job, lease = queue_service.claim(session, node_id=config.node_id)
        session.commit()
        if job is None:
            break
        job_id = job.job_id
        channel = _EventChannel(session, wal, job, lease)
        try:
            if job.kind == "PREPARE":
                _do_prepare(session, job, channel, adapter, config)
            elif job.kind == "EXECUTE":
                _do_execute(session, job, channel, adapter, config)
            else:
                channel.post("FAILED", {"reason": f"未知 Job.kind: {job.kind}", "exit_code": 64})
        except Exception as exc:  # 失败不删除：如实 FAILED 事件 + 现场保留
            session.rollback()
            session.invalidate()
            job = session.get(JobRow, job_id)
            lease = (
                session.query(LeaseRow)
                .filter_by(job_id=job_id, active=True)
                .order_by(LeaseRow.fencing_token.desc())
                .first()
            )
            if job is not None and lease is not None:
                channel = _EventChannel(session, wal, job, lease)
                if job.state == "LEASED":
                    channel.post("STARTING", {"stage": "worker_initialization", "launch_intent_id": f"intent-{job_id}"})
                channel.post(
                    "FAILED",
                    {"reason": f"Worker 内部异常: {type(exc).__name__}: {exc}", "exit_code": 1},
                )
        processed.append(job_id)
        count += 1
    return processed


__all__ = [
    "WorkerConfig",
    "make_adapter_from_env",
    "replay_wal",
    "run_until_idle",
]
