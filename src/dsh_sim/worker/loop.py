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
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from dsh_sim.adapters.star_adapter import ExecutionBudget, StarAdapter
from dsh_sim.canonical import canonical_dumps, sha256_hex
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
from dsh_sim.resources import state_dir
from dsh_sim.worker.wal import JsonlWal

# ---------------------------------------------------------------------------
# 配置（DSH_SIM_WORKER_* env；不写死）
# ---------------------------------------------------------------------------

ENV_NODE_ID = "DSH_SIM_WORKER_NODE_ID"
ENV_WORK_DIR = "DSH_SIM_WORKER_WORK_DIR"
ENV_MAX_JOBS = "DSH_SIM_WORKER_MAX_JOBS"
ENV_MAX_POLLS = "DSH_SIM_WORKER_MAX_POLLS"
ENV_ARTIFACT_ROOT = "DSH_SIM_ARTIFACT_ROOT"


@dataclass(frozen=True)
class WorkerConfig:
    node_id: str
    work_root: Path
    artifact_root: Path
    max_jobs: int | None = None  # None = 不限；测试用小值防死循环
    max_polls: int = 32  # poll 异常保护上限（hang 行为只应被 cancel 终结）

    @classmethod
    def from_env(cls, repo_root: Path | None = None) -> "WorkerConfig":
        """默认工作/工件目录。

        `repo_root` 显式给出时保持历史语义（`<repo_root>/var/{worker,artifacts}`）；
        否则走状态目录解析（仓库 `var/` 或安装后的用户状态目录），不再用
        `parents[3]` 推算仓库根——wheel 安装后那会落到 site-packages 上层
        （上游验收报告 §五）。
        """
        if repo_root is not None:
            base = repo_root / "var"
        else:
            base = state_dir()
        return cls(
            node_id=os.environ.get(ENV_NODE_ID, f"node-{uuid.uuid4().hex[:8]}"),
            work_root=Path(os.environ.get(ENV_WORK_DIR, str(base / "worker"))),
            artifact_root=Path(os.environ.get(ENV_ARTIFACT_ROOT, str(base / "artifacts"))),
            max_jobs=(
                int(os.environ[ENV_MAX_JOBS]) if os.environ.get(ENV_MAX_JOBS) else None
            ),
            max_polls=int(os.environ.get(ENV_MAX_POLLS, "32")),
        )


def make_adapter_from_env() -> StarAdapter:
    """DSH_SIM_WORKER_ADAPTER=mock（默认，离线开发）| cli（真实支，需探针解锁）。"""
    kind = os.environ.get("DSH_SIM_WORKER_ADAPTER", "mock")
    if kind == "mock":
        from dsh_sim.adapters.mock_adapter import MockStarAdapter

        return MockStarAdapter(
            behavior=os.environ.get("DSH_SIM_WORKER_MOCK_BEHAVIOR", "staged")  # type: ignore[arg-type]
        )
    if kind == "cli":
        from dsh_sim.adapters.cli_adapter import StarCliAdapter

        return StarCliAdapter()
    raise ValueError(f"未知 DSH_SIM_WORKER_ADAPTER: {kind!r}（只允许 mock/cli）")


# ---------------------------------------------------------------------------
# 事件上报（WAL 先行）
# ---------------------------------------------------------------------------


class _EventChannel:
    def __init__(self, session: Session, wal: JsonlWal, job: JobRow, lease: LeaseRow) -> None:
        self._session = session
        self._wal = wal
        self.job = job
        self.lease = lease
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
            job_id=self.job.job_id,
            lease_id=self.lease.lease_id,
            fencing_token=self.lease.fencing_token,
            event_seq=seq,
            kind=kind,
            payload=payload,
        )
        queue_service.post_event(
            self._session,
            job_id=self.job.job_id,
            lease_id=self.lease.lease_id,
            fencing_token=self.lease.fencing_token,
            event_seq=seq,
            kind=kind,
            payload=payload,
        )
        self._session.commit()  # 事件增量持久化：断线后重进可见相同状态（FR-11）
        self._wal.mark_acked(self.job.job_id, seq)
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
) -> ArtifactRow:
    content = src_path.read_bytes()
    sha = hashlib.sha256(content).hexdigest()
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
    probe = adapter.probe_environment()

    channel.post(
        "STARTING",
        {
            "preparation_id": prep.preparation_id,
            "launch_intent_id": f"intent-{job.job_id}",
            "work_dir_summary": sha256_hex(str(config.work_root / task.task_id))[:16],
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
                / variant["variant_id"]
                / condition["condition_id"]
            )
            case = adapter.prepare_case(
                template_ref=variant["template_artifact_id"],
                whitelist_writes=_whitelist_writes(condition),
                work_dir=str(cell_dir),
            )
            readback = adapter.read_actual_settings(case.prepared_sim_path)
            readback_shas.append(readback.readback_sha)

            logical = (
                f"prepared/{variant['variant_id']}/{condition['condition_id']}/"
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

    channel.post(
        "RUNNING",
        {
            "process_identity": f"mock-prepare:{job.job_id}",
            "software_build": probe.star_build,
            "stage": "readback",
        },
    )

    # 语义 finding 也作为差异阻塞（不静默跳过）
    for f_ in semantic_findings:
        differences.append({"field": f_["kind"], "status": "MISMATCH", "requested": None, "readback": None, "unit": "", "boundary": "", "message": f_["message"]})

    combined_readback_sha = sha256_hex(canonical_dumps(readback_shas))
    from dsh_sim.api.services.prep_service import mark_preparation_ready

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


def _find_prepared_path(
    session: Session,
    prep: PreparationRow | None,
    variant_id: str,
    condition_id: str,
) -> str | None:
    if prep is None:
        return None
    for logical in prep.prepared_artifacts:
        if f"/{variant_id}/{condition_id}/" in logical:
            art = (
                session.query(ArtifactRow)
                .filter_by(logical_path=logical, state="COMMITTED")
                .first()
            )
            if art is not None and art.storage_path and Path(art.storage_path).is_file():
                return art.storage_path
    return None


def _do_execute(
    session: Session,
    job: JobRow,
    channel: _EventChannel,
    adapter: StarAdapter,
    config: WorkerConfig,
) -> None:
    run = session.get(RunRow, job.run_id)
    attempt = session.get(AttemptRow, job.attempt_id)
    task = session.get(TaskRow, run.task_id)
    rev = (
        session.query(TaskRevisionRow)
        .filter_by(task_id=task.task_id, revision=run.revision)
        .first()
    )
    spec = rev.spec
    probe = adapter.probe_environment()
    mode = probe.evidence_mode
    infix = ".mock" if mode == "MOCK" else ""

    work_dir = config.work_root / run.run_id / f"attempt-{attempt.attempt_no}"
    work_dir.mkdir(parents=True, exist_ok=True)

    prep = (
        session.query(PreparationRow)
        .filter_by(task_id=task.task_id, revision=run.revision)
        .order_by(PreparationRow.created_at.desc())
        .first()
    )
    prepared_ref = _find_prepared_path(session, prep, run.variant_id, run.condition_id)
    if prepared_ref is None:
        channel.post(
            "FAILED",
            {"reason": "prepared 产物缺失（准备链未交付该工况），不补造", "exit_code": 70},
        )
        return

    # 输入 staging：把 artifact 存储中的 prepared 副本复制进作业工作目录后启动，
    # 产物全部落在本 attempt 工作目录（真实支同理：不就地改 artifact 存储）。
    staged_prepared = work_dir / Path(prepared_ref).name
    staged_prepared.write_bytes(Path(prepared_ref).read_bytes())

    channel.post(
        "STARTING",
        {
            "attempt_id": attempt.attempt_id,
            "launch_intent_id": f"intent-{job.job_id}",
            "work_dir_summary": sha256_hex(str(work_dir))[:16],
            "prepared_ref_sha": hashlib.sha256(staged_prepared.read_bytes()).hexdigest(),
            "evidence_mode": mode,
        },
    )

    auth = (
        session.query(AuthorizationRow)
        .filter_by(task_id=task.task_id, revision=run.revision, validity="CURRENT")
        .order_by(AuthorizationRow.created_at.desc())
        .first()
    )
    budget_spec = (auth.execution_budget if auth else spec.get("execution_budget")) or {}
    budget = ExecutionBudget(
        max_iterations=None,
        wall_clock_seconds=int(budget_spec.get("wallclock_hours", 1) * 3600),
        cpu_cores=int(budget_spec.get("cpu_cores", 1)),
        attempt_no=attempt.attempt_no,
    )

    handle = adapter.launch(str(staged_prepared), budget)
    # JobHandle.run_id 由适配器从工作目录解析；保持一致性校验（不猜）
    channel.post(
        "RUNNING",
        {
            "process_identity": handle.process_identity,
            "software_build": probe.star_build,
            "stage": "launch",
            "evidence_mode": mode,
        },
    )

    # poll 循环：心跳载荷含真实阶段/日志位置/资源状态；取消请求异步确认
    terminal = {"SUCCEEDED", "FAILED", "CANCELLED"}
    phase = "RUNNING"
    for _ in range(config.max_polls):
        session.refresh(job)
        if job.cancel_requested:
            status = adapter.cancel(handle)
            channel.post(
                "CANCELLED",
                {
                    "reason": "用户取消请求，受控进程组退出（现场确认）",
                    "exit_proof": status.detail.get("exit_proof"),
                    "exit_code": status.detail.get("exit_code", 130),
                    "evidence_mode": mode,
                },
            )
            return
        status = adapter.poll(handle)
        phase = status.phase
        if phase not in terminal:
            channel.post(
                "HEARTBEAT",
                {
                    "stage": phase,
                    "log_location": status.log_location,
                    "resource_state": {"cpu_cores": budget.cpu_cores},
                    "evidence_mode": mode,
                },
            )
        else:
            break
    if phase not in terminal:
        channel.post(
            "FAILED",
            {"reason": f"poll 超过保护上限 {config.max_polls}（异常保护，不冒充完成）", "exit_code": 71},
        )
        return
    if phase == "FAILED":
        channel.post(
            "FAILED",
            {"reason": "求解器报告失败", "exit_code": status.detail.get("exit_code", 2), "evidence_mode": mode},
        )
        return
    if phase == "CANCELLED":
        channel.post(
            "CANCELLED",
            {"reason": "适配器报告已取消", "exit_code": status.detail.get("exit_code", 130), "evidence_mode": mode},
        )
        return

    # COLLECTING：收集原始产物 → 登记 artifact（摘要核对）→ 提取 → 独立校核
    collected = adapter.collect_outputs(handle)
    artifact_ids: list[str] = []
    report_art = monitor_art = None
    for logical, expected_sha in collected.artifact_digests.items():
        local = Path(work_dir) / Path(logical).name
        actual_sha = hashlib.sha256(local.read_bytes()).hexdigest()
        if actual_sha != expected_sha:
            channel.post(
                "FAILED",
                {"reason": f"产物摘要核验失败 {logical}（证据故障）", "exit_code": 72},
            )
            return
        art = _commit_artifact(
            session,
            project_id=task.project_id,
            job_id=job.job_id,
            logical_path=f"runs/{run.run_id}/attempt-{attempt.attempt_no}/{logical}",
            src_path=local,
            artifact_root=config.artifact_root,
            evidence_mode=collected.evidence_mode,
            run_id=run.run_id,
            attempt_id=attempt.attempt_id,
        )
        artifact_ids.append(art.artifact_id)
        if "report" in logical:
            report_art = art
        if "monitor" in logical:
            monitor_art = art

    # 固定提取器结构化原始指标输入 → 存为指标 artifact（缺值为 null，不填 0）
    raw_metrics = adapter.extract_metrics(collected)
    metrics_path = work_dir / f"metrics{infix}.json"
    import json as _json

    metrics_body = {
        "evidence_mode": mode,
        "extractor_id": raw_metrics.extractor_id,
        "extractor_version": raw_metrics.extractor_version,
        "metric_values": raw_metrics.metric_values,
        "missing_metrics": list(raw_metrics.missing_metrics),
        "source_artifacts": raw_metrics.source_artifacts,
    }
    banner = "# MOCK DATA - NOT REAL SOLVER OUTPUT\n" if mode == "MOCK" else ""
    metrics_path.write_text(
        banner + _json.dumps(metrics_body, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    metrics_art = _commit_artifact(
        session,
        project_id=task.project_id,
        job_id=job.job_id,
        logical_path=f"runs/{run.run_id}/attempt-{attempt.attempt_no}/metrics{infix}.json",
        src_path=metrics_path,
        artifact_root=config.artifact_root,
        evidence_mode=mode,
        run_id=run.run_id,
        attempt_id=attempt.attempt_id,
    )
    artifact_ids.append(metrics_art.artifact_id)

    # 独立校核（FR-15/16）：从原始 CSV 复算，不照抄报告；阈值 null → UNCONFIRMED
    method = spec.get("method") or {}
    pkg_id = method.get("capability_package_id", "buffer_chamber")
    pkg_ver = "0.1.0"
    rules, rule_set_sha = load_rule_set(pkg_id, pkg_ver)
    metric_defs = load_metric_definitions(pkg_id, pkg_ver)
    domain = load_domain(pkg_id, pkg_ver)
    extracted = extract_run_metrics(
        report_art.storage_path if report_art else None,
        monitor_art.storage_path if monitor_art else None,
        metric_definitions=metric_defs,
    )
    extracted.findings.extend(check_unit_semantics(spec))
    result = verify_run(
        extracted=extracted,
        rules=rules,
        domain=domain,
        required_metrics=method.get("required_metrics", []),
    )
    ver = persist_verification(
        session,
        run=run,
        attempt_id=attempt.attempt_id,
        result=result,
        rule_set_sha256=rule_set_sha,
        source_artifact_ids=[a for a in (report_art and report_art.artifact_id, monitor_art and monitor_art.artifact_id, metrics_art.artifact_id) if a],
    )
    session.commit()

    channel.post(
        "COMPLETED",
        {
            "exit_code": 0,
            "artifact_ids": artifact_ids,
            "verification_id": ver.verification_id,
            "numerical_conclusion": result.conclusion,
            "applicability": result.applicability,
            "evidence_mode": mode,
        },
    )


# ---------------------------------------------------------------------------
# 领取循环入口
# ---------------------------------------------------------------------------


def run_until_idle(
    session: Session,
    adapter: StarAdapter,
    config: WorkerConfig,
) -> list[str]:
    """顺序领取并处理作业直到队列空。返回处理的 job_id 列表（测试可断言）。"""
    wal = JsonlWal(config.work_root / "worker.wal.jsonl")
    replay_wal(session, wal)
    processed: list[str] = []
    count = 0
    while True:
        if config.max_jobs is not None and count >= config.max_jobs:
            break
        job, lease = queue_service.claim(session, node_id=config.node_id)
        session.commit()
        if job is None:
            break
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
            job = session.get(JobRow, job.job_id)
            lease = (
                session.query(LeaseRow)
                .filter_by(job_id=job.job_id, active=True)
                .order_by(LeaseRow.fencing_token.desc())
                .first()
            )
            if job is not None and lease is not None:
                channel = _EventChannel(session, wal, job, lease)
                channel.post(
                    "FAILED",
                    {"reason": f"Worker 内部异常: {type(exc).__name__}: {exc}", "exit_code": 1},
                )
        processed.append(job.job_id)
        count += 1
    return processed


__all__ = [
    "WorkerConfig",
    "make_adapter_from_env",
    "replay_wal",
    "run_until_idle",
]
