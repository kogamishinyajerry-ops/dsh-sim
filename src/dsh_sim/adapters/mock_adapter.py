"""MockStarAdapter —— 协议/控制逻辑开发与故障注入用 Mock（定义书 §测试分层）。

诚实红线（CONVENTIONS §0.1）：
- 本适配器产生的一切数据/文件/状态 evidence_mode 恒为 "MOCK"，绝不冒充 REAL。
- 生成的假 CSV/报告文件名带 `.mock.` 中缀；内容首行固定
  `# MOCK DATA - NOT REAL SOLVER OUTPUT`。
- 工程阈值不出现在本文件；Mock 只提供"结构正确"的原始数值载体。

内存状态机模拟阶段推进，行为可配置（behavior）：
- "immediate"：launch 后第一次 poll 即 SUCCEEDED；
- "staged"：STARTING → RUNNING → COLLECTING → SUCCEEDED 逐 poll 推进（默认）；
- "fail"：RUNNING 阶段后转 FAILED（exit_code=2）；
- "hang"：停在 RUNNING 不推进（供失联/取消测试）。
回读差异注入：构造参数 mismatch_fields 中的 field_id 会在 read_actual_settings
中以 MISMATCH 返回（申请值 +1.0），供准备差异链测试。
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from dsh_sim.adapters.star_adapter import (
    BoundaryRoleCandidate,
    CollectedOutputs,
    EnvironmentProbe,
    ExecutionBudget,
    JobHandle,
    JobStatus,
    LicenseStatus,
    PreparedCase,
    RawMetrics,
    ReadbackEntry,
    ReadbackSet,
    TemplateInspection,
    WhitelistWrite,
)
from dsh_sim.canonical import canonical_dumps, sha256_hex

MOCK_BANNER = "# MOCK DATA - NOT REAL SOLVER OUTPUT"
MOCK_BUILD = "MOCK-STAR-CCM+ 0.0.0-mock（非真实求解器构建）"

Behavior = Literal["immediate", "staged", "fail", "hang"]

# Mock 报告 CSV 的固定列（与 verify/extract.py 的独立复算解析约定一致）
REPORT_HEADER = (
    "section,boundary_role,sign_convention,mass_flow_kg_s,"
    "total_pressure_pa,static_pressure_pa"
)
# 确定性 Mock 原始数值（质量守恒：-1.2 + 0.7 + 0.5 = 0）：
# 入口向外为负、出口为正（定义书 §指标定义 边界质量流量符号约定）。
_MOCK_ROWS = (
    ("inlet", "inlet", -1.2, 101500.0, 101000.0),
    ("outlet", "outlet_a", 0.7, 100900.0, 100650.0),
    ("outlet", "outlet_b", 0.5, 100900.0, 100650.0),
)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass
class _MockJob:
    handle: JobHandle
    behavior: Behavior
    stage_index: int = 0
    cancelled: bool = False


class MockStarAdapter:
    """StarAdapter 接口的 Mock 实现。全部产物 evidence_mode="MOCK"。"""

    adapter_build = "mock-adapter/0.1.0"

    def __init__(
        self,
        *,
        behavior: Behavior = "staged",
        mismatch_fields: set[str] | None = None,
    ) -> None:
        self.behavior: Behavior = behavior
        self.mismatch_fields = set(mismatch_fields or ())
        self._jobs: dict[str, _MockJob] = {}

    # ------------------------------------------------------------------
    # 环境探针
    # ------------------------------------------------------------------

    def probe_environment(self) -> EnvironmentProbe:
        return EnvironmentProbe(
            star_build=MOCK_BUILD,
            executable_path="mock://starccm+（无真实可执行程序）",
            version_features=(),
            license_status=LicenseStatus.UNCONFIRMED,
            license_details={"note": "MOCK 探针不代表真实授权状态"},
            probed_at=_utcnow_iso(),
            evidence_mode="MOCK",
        )

    # ------------------------------------------------------------------
    # 模板检查
    # ------------------------------------------------------------------

    def inspect_template(self, template_ref: str) -> TemplateInspection:
        return TemplateInspection(
            template_ref=template_ref,
            boundary_role_candidates=(
                BoundaryRoleCandidate("inlet", "速度入口(MOCK)", "region_fluid", {}),
                BoundaryRoleCandidate("outlet_a", "压力出口(MOCK)", "region_fluid", {}),
                BoundaryRoleCandidate("outlet_b", "压力出口(MOCK)", "region_fluid", {}),
            ),
            regions=("region_fluid",),
            physics_models=("steady_single_phase_internal_flow(MOCK)",),
            mesh_summary={"note": "MOCK 网格摘要，非真实枚举"},
            report_definitions=("mass_flow_report(MOCK)", "total_pressure_report(MOCK)"),
            class_name_guess_warning=(
                "MOCK 枚举不代表真实模板内容；真实边界/模型须经 P04 角色绑定探针确认"
            ),
            evidence_mode="MOCK",
        )

    # ------------------------------------------------------------------
    # 准备与回读
    # ------------------------------------------------------------------

    def prepare_case(
        self,
        template_ref: str,
        whitelist_writes: list[WhitelistWrite],
        work_dir: str,
    ) -> PreparedCase:
        work = Path(work_dir)
        work.mkdir(parents=True, exist_ok=True)
        # 不覆盖源模板：源模板摘要即 template_ref 中声明的 sha（spec.template_sha256），
        # Mock 无真实模板文件，以 ref 字符串派生占位源摘要并显式标注。
        source_sha = sha256_hex(f"mock-template:{template_ref}")
        prepared_path = work / "prepared.mock.sim"
        payload = {
            "banner": MOCK_BANNER,
            "evidence_mode": "MOCK",
            "template_ref": template_ref,
            "source_template_sha256": source_sha,
            "writes": [
                {
                    "field_id": w.field_id,
                    "boundary_role": w.boundary_role,
                    "value_si": w.value_si,
                    "unit": w.unit,
                    "source_ref": w.source_ref,
                }
                for w in whitelist_writes
            ],
        }
        prepared_path.write_text(
            MOCK_BANNER + "\n" + json.dumps(payload, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        return PreparedCase(
            prepared_sim_path=str(prepared_path),
            prepared_sha256=_file_sha256(prepared_path),
            source_template_sha256=source_sha,
            writes_applied=tuple(whitelist_writes),
            summary={
                "evidence_mode": "MOCK",
                "writes_count": len(whitelist_writes),
                "note": "MOCK 准备产物，不含真实求解器设置",
            },
            evidence_mode="MOCK",
        )

    def read_actual_settings(self, prepared_ref: str) -> ReadbackSet:
        path = Path(prepared_ref)
        raw = path.read_text(encoding="utf-8")
        payload = json.loads(raw.split("\n", 1)[1])
        entries: list[ReadbackEntry] = []
        missing: list[str] = []
        mismatched: list[str] = []
        for w in payload["writes"]:
            fid = w["field_id"]
            requested = float(w["value_si"])
            actual = requested + 1.0 if fid in self.mismatch_fields else requested
            status: Literal["MATCH", "MISSING", "MISMATCH"] = (
                "MISMATCH" if fid in self.mismatch_fields else "MATCH"
            )
            if status == "MISMATCH":
                mismatched.append(fid)
            entries.append(
                ReadbackEntry(
                    field_id=fid,
                    requested_value_si=requested,
                    actual_value_si=actual,
                    status=status,
                )
            )
        readback_sha = sha256_hex(
            canonical_dumps(
                [
                    {
                        "field_id": e.field_id,
                        "requested_value_si": e.requested_value_si,
                        "actual_value_si": e.actual_value_si,
                        "status": e.status,
                    }
                    for e in entries
                ]
            )
        )
        return ReadbackSet(
            entries=tuple(entries),
            missing_fields=tuple(missing),
            mismatched_fields=tuple(mismatched),
            model_identities={"solver": MOCK_BUILD, "physics": "steady_single_phase(MOCK)"},
            readback_sha=readback_sha,
            evidence_mode="MOCK",
        )

    # ------------------------------------------------------------------
    # 启动 / 轮询 / 取消（内存状态机）
    # ------------------------------------------------------------------

    def launch(self, prepared_ref: str, budget: ExecutionBudget) -> JobHandle:
        job_id = f"mockjob_{uuid.uuid4().hex[:16]}"
        work_dir = str(Path(prepared_ref).parent)
        # run_id 由 Worker 编码进工作目录名（work_root/<run_id>/attempt-<n>），
        # Mock 从路径尽力解析；解析不到时显式 "run_unknown"，不猜。
        run_id = next(
            (p for p in Path(prepared_ref).parts if re.fullmatch(r"run_[0-9a-f]{24}", p)),
            "run_unknown",
        )
        handle = JobHandle(
            job_id=job_id,
            run_id=run_id,
            attempt_no=budget.attempt_no,
            process_identity=f"mock-pid:{uuid.uuid4().hex[:8]}",
            work_dir=work_dir,
            launch_args=("mock://launch", prepared_ref, f"--attempt={budget.attempt_no}"),
        )
        self._jobs[job_id] = _MockJob(handle=handle, behavior=self.behavior)
        return handle

    def poll(self, job_handle: JobHandle) -> JobStatus:
        job = self._jobs[job_handle.job_id]
        if job.cancelled:
            phase = "CANCELLED"
        elif job.behavior == "immediate":
            phase = "SUCCEEDED"
        elif job.behavior == "hang":
            phase = "RUNNING"  # 永不推进，供失联/取消测试
        else:
            stages = ["STARTING", "RUNNING", "COLLECTING", "SUCCEEDED"]
            idx = min(job.stage_index, len(stages) - 1)
            phase = stages[idx]
            if job.behavior == "fail" and phase in ("COLLECTING", "SUCCEEDED"):
                phase = "FAILED"
            job.stage_index += 1
        return JobStatus(
            job_id=job_handle.job_id,
            phase=phase,
            heartbeat_at=_utcnow_iso(),
            log_location=str(Path(job_handle.work_dir) / "run.mock.log"),
            detail={
                "evidence_mode": "MOCK",
                "exit_code": 2 if phase == "FAILED" else (0 if phase == "SUCCEEDED" else None),
                "process_identity": job_handle.process_identity,
            },
        )

    def cancel(self, job_handle: JobHandle) -> JobStatus:
        job = self._jobs[job_handle.job_id]
        job.cancelled = True
        return JobStatus(
            job_id=job_handle.job_id,
            phase="CANCELLED",
            heartbeat_at=_utcnow_iso(),
            log_location=str(Path(job_handle.work_dir) / "run.mock.log"),
            detail={
                "evidence_mode": "MOCK",
                "exit_proof": "mock 进程组已受控退出（内存状态机现场确认）",
                "exit_code": 130,
            },
        )

    # ------------------------------------------------------------------
    # 产物收集与指标提取
    # ------------------------------------------------------------------

    def collect_outputs(self, job_handle: JobHandle) -> CollectedOutputs:
        work = Path(job_handle.work_dir)
        work.mkdir(parents=True, exist_ok=True)

        report = work / "report.mock.csv"
        lines = [
            MOCK_BANNER,
            "# evidence_mode: MOCK",
            REPORT_HEADER,
        ]
        for section, role, m, tp, sp in _MOCK_ROWS:
            lines.append(f"{section},{role},outward_positive,{m},{tp},{sp}")
        report.write_text("\n".join(lines) + "\n", encoding="utf-8")

        monitor = work / "monitor.mock.csv"
        mlines = [
            MOCK_BANNER,
            "# evidence_mode: MOCK",
            "iteration,continuity,mass_imbalance_watch",
            "1,1.0e-3,0.0",
            "2,5.0e-4,0.0",
            "3,2.5e-4,0.0",
        ]
        monitor.write_text("\n".join(mlines) + "\n", encoding="utf-8")

        result_sim = work / "result.mock.sim"
        result_sim.write_text(
            MOCK_BANNER + "\n"
            + json.dumps({"evidence_mode": "MOCK", "note": "MOCK 求解结果占位"}, ensure_ascii=False),
            encoding="utf-8",
        )

        digests = {
            f"raw/{report.name}": _file_sha256(report),
            f"raw/{monitor.name}": _file_sha256(monitor),
            f"raw/{result_sim.name}": _file_sha256(result_sim),
        }
        return CollectedOutputs(
            run_id=job_handle.run_id,
            attempt_no=job_handle.attempt_no,
            raw_reports=(str(report),),
            monitors_csv=(str(monitor),),
            scenes=(),
            result_sim=str(result_sim),
            artifact_digests=digests,
            summary={"evidence_mode": "MOCK", "files": len(digests)},
            evidence_mode="MOCK",
        )

    def extract_metrics(self, collected: CollectedOutputs) -> RawMetrics:
        """固定提取器：解析 mock 报告 CSV 为结构化原始指标输入。

        只提取"原始边界量"（各角色质量流量/总压），派生指标（压损/分配/不平衡）
        由 verify/extract.py 独立复算（ADR-09，不照抄报告）。
        """
        values: dict[str, float | None] = {}
        sources: dict[str, str] = {}
        missing: list[str] = []
        for path in collected.raw_reports:
            sha = _file_sha256(Path(path))
            for row in _parse_report_csv(Path(path)):
                role = row["boundary_role"]
                values[f"boundary_mass_flow@{role}"] = row["mass_flow_kg_s"]
                values[f"total_pressure@{role}"] = row["total_pressure_pa"]
                sources[f"boundary_mass_flow@{role}"] = sha
                sources[f"total_pressure@{role}"] = sha
        if not values:
            missing.append("boundary_mass_flow")
        return RawMetrics(
            metric_values=values,
            missing_metrics=tuple(missing),
            extractor_id="mock-fixed-extractor",
            extractor_version="0.1.0",
            source_artifacts=sources,
            evidence_mode="MOCK",
        )


def _parse_report_csv(path: Path) -> list[dict[str, float | str]]:
    """解析固定列的报告 CSV（跳过 # 注释行）。缺值/非数值抛 ValueError。"""
    rows: list[dict[str, float | str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line == REPORT_HEADER:
            continue
        parts = line.split(",")
        if len(parts) != 6:
            raise ValueError(f"报告 CSV 行列数不符: {line!r}")
        section, role, sign, m, tp, sp = parts
        rows.append(
            {
                "section": section,
                "boundary_role": role,
                "sign_convention": sign,
                "mass_flow_kg_s": float(m),
                "total_pressure_pa": float(tp),
                "static_pressure_pa": float(sp),
            }
        )
    return rows


__all__ = ["MOCK_BANNER", "MOCK_BUILD", "MockStarAdapter"]
