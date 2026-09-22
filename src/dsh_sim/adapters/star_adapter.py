"""dsh-sim STAR-CCM+ 适配器接口层（定义书 §STAR-CCM+适配器定义）。

接口冻结，实现分 MockStarAdapter / StarCliAdapter 两支：
- MockStarAdapter：协议/控制逻辑开发与故障注入；所有产物必须带 evidence_mode="MOCK"（CONVENTIONS §0.1）。
- StarCliAdapter：经 <STARCCM_CLI_DIR>\\starccm_cli.py 桥与真实 STAR-CCM+ 交互；
  须遵守探针实测约束（compatibility/probe-report.md）：
  spawn 语法 [bat, sim, "-batch", macro]（sim 在前），-batch 后拒绝额外位置参数，
  宏参数烘焙进 .java 源码，spawn 前设 JAVA_TOOL_OPTIONS=-Dfile.encoding=UTF-8。

本文件只含类型与接口，无任何实现。工程阈值（容差/窗口/上下界）一律不出现
在本文件中——它们是 Owner 冻结项（CONVENTIONS §0.3）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Literal, Protocol


# ---------------------------------------------------------------------------
# 公共类型
# ---------------------------------------------------------------------------

EvidenceMode = Literal["MOCK", "REAL"]
"""MOCK 绝不冒充 REAL：所有 Mock 产生的数据/状态/报告，字段 evidence_mode 必带。"""


class LicenseStatus(str, Enum):
    """授权状态；无法无交互确认时必须为 UNCONFIRMED，不准猜。"""

    LICENSED = "LICENSED"
    UNLICENSED = "UNLICENSED"
    PARTIAL = "PARTIAL"  # 例: ccmpsuite_solve 可用但 ccmpsuite_init 缺失
    UNCONFIRMED = "UNCONFIRMED"


@dataclass(frozen=True)
class EnvironmentProbe:
    """probe_environment 输出。"""

    star_build: str  # 真实构建号, 例 "Simcenter STAR-CCM+ 2402 Build 19.02.009 (...)"
    executable_path: str  # starccm+.bat 真实路径
    version_features: tuple[str, ...]  # 实测支持的版本特性(由探针证据支撑, 非推断)
    license_status: LicenseStatus
    license_details: dict[str, object]  # 例 available/missing features; 不得编造
    probed_at: str  # ISO 8601 探针时间
    evidence_mode: EvidenceMode
    evidence_refs: tuple[str, ...] = ()  # 探针证据文件路径


@dataclass(frozen=True)
class BoundaryRoleCandidate:
    """边界角色候选：真实枚举的边界，附签名；角色归属由批准 boundary-map 决定。"""

    boundary_name: str
    boundary_type: str  # 软件返回的类型展示名, 例 "速度入口"
    region_name: str
    signature: dict[str, object]  # 面积/质心等实测签名; 未采集则为空 dict


@dataclass(frozen=True)
class TemplateInspection:
    """inspect_template 输出。"""

    template_ref: str  # 模板 artifact 引用(内容摘要标识)
    boundary_role_candidates: tuple[BoundaryRoleCandidate, ...]
    regions: tuple[str, ...]
    physics_models: tuple[str, ...]  # 真实枚举的模型名
    mesh_summary: dict[str, object]  # region/boundary/part 计数等实测事实
    report_definitions: tuple[str, ...]
    class_name_guess_warning: str  # "猜测类名不是能力"告警字段: 凡未经实测枚举的条目必须在此声明
    evidence_mode: EvidenceMode


@dataclass(frozen=True)
class WhitelistWrite:
    """一次白名单写入：仅允许能力包白名单内的工况字段。"""

    field_id: str  # 能力包白名单字段 ID
    boundary_role: str  # 工程角色 role_id
    value_si: float
    unit: str
    source_ref: str  # 工况来源引用


@dataclass(frozen=True)
class PreparedCase:
    """prepare_case 输出。"""

    prepared_sim_path: str  # 独立工作副本路径(prepared.sim)
    prepared_sha256: str
    source_template_sha256: str  # 源模板摘要, 用于证明原件未被覆盖
    writes_applied: tuple[WhitelistWrite, ...]
    summary: dict[str, object]  # 准备摘要(供人工确认面板展示)
    evidence_mode: EvidenceMode


@dataclass(frozen=True)
class ReadbackEntry:
    field_id: str
    requested_value_si: float | None
    actual_value_si: float | None  # 从真实软件读到的生效值
    status: Literal["MATCH", "MISSING", "MISMATCH"]


@dataclass(frozen=True)
class ReadbackSet:
    """read_actual_settings 输出：独立回读集合，含缺失/错误字段数组。"""

    entries: tuple[ReadbackEntry, ...]
    missing_fields: tuple[str, ...]  # 写入被忽略/读不到的字段
    mismatched_fields: tuple[str, ...]  # 申请值与真实值不一致的字段
    model_identities: dict[str, str]  # 物理模型/求解器真实身份
    readback_sha: str  # 回读集合摘要(进入 prepared_digest)
    evidence_mode: EvidenceMode


@dataclass(frozen=True)
class ExecutionBudget:
    """运行预算(来自 authorizeRuns 的批准值)。"""

    max_iterations: int | None
    wall_clock_seconds: int | None
    cpu_cores: int
    attempt_no: int


@dataclass(frozen=True)
class JobHandle:
    """launch 返回的作业句柄。"""

    job_id: str
    run_id: str
    attempt_no: int
    process_identity: str | None  # 进程身份/创建时间或外部调度 ID
    work_dir: str
    launch_args: tuple[str, ...]  # argv 结构化(list[str] 语义), 禁止 shell 拼接后的单一命令串


@dataclass(frozen=True)
class JobStatus:
    """poll 输出。"""

    job_id: str
    phase: str  # 真实阶段, 例 STARTING/RUNNING/COLLECTING/...
    heartbeat_at: str | None
    log_location: str | None
    detail: dict[str, object]


@dataclass(frozen=True)
class CollectedOutputs:
    """collect_outputs 输出。"""

    run_id: str
    attempt_no: int
    raw_reports: tuple[str, ...]  # 原始报告 artifact 路径
    monitors_csv: tuple[str, ...]  # 监控/残差原始 CSV
    scenes: tuple[str, ...]
    result_sim: str | None  # 求解后 .sim
    artifact_digests: dict[str, str]  # logical_path -> sha256; 上传后校验摘要
    summary: dict[str, object]
    evidence_mode: EvidenceMode


@dataclass(frozen=True)
class RawMetrics:
    """extract_metrics 输出：固定提取器结果；缺值为 None 且标缺失，不填 0。"""

    metric_values: dict[str, float | None]  # metric_id -> SI 值; 缺值 None
    missing_metrics: tuple[str, ...]
    extractor_id: str  # 固定提取器身份(名称+版本)
    extractor_version: str
    source_artifacts: dict[str, str]  # metric_id -> 来源 artifact 摘要
    evidence_mode: EvidenceMode


# ---------------------------------------------------------------------------
# 适配器接口（7 个，docstring 抄定义书 §STAR-CCM+适配器定义 对应约束原文）
# ---------------------------------------------------------------------------


class StarAdapter(Protocol):
    """STAR-CCM+ 适配器接口。两支实现：MockStarAdapter / StarCliAdapter。"""

    def probe_environment(self) -> EnvironmentProbe:
        """读取真实构建号、可执行程序路径、版本特性及测试结果；无合法授权不得继续。"""
        ...

    def inspect_template(self, template_ref: str) -> TemplateInspection:
        """返回边界角色候选、区域/模型、网格与报告定义；不得将猜测类名当作能力。"""
        ...

    def prepare_case(
        self,
        template_ref: str,
        whitelist_writes: list[WhitelistWrite],
        work_dir: str,
    ) -> PreparedCase:
        """复制批准模板，写白名单参数，保存prepared.sim；不覆盖源模板、不启动正式迭代。"""
        ...

    def read_actual_settings(self, prepared_ref: str) -> ReadbackSet:
        """从真实软件读取设置与模型身份；输出独立ReadbackSet，包含缺失/错误字段。"""
        ...

    def launch(self, prepared_ref: str, budget: ExecutionBudget) -> JobHandle:
        """受控启动；argv结构化（args: list[str]），禁止shell拼接命令。"""
        ...

    def poll(self, job_handle: JobHandle) -> JobStatus:
        """状态采集；argv结构化，禁止shell拼接命令。"""
        ...

    def cancel(self, job_handle: JobHandle) -> JobStatus:
        """进程组取消；argv结构化，禁止shell拼接命令；无法证实退出时由上层标记 LOST。"""
        ...

    def collect_outputs(self, job_handle: JobHandle) -> CollectedOutputs:
        """保存原始报告、监控、场景及.sim结果，标识run/attempt；上传后校验摘要。"""
        ...

    def extract_metrics(self, collected: CollectedOutputs) -> RawMetrics:
        """固定提取器从原始数据生成指标；不让模型看图估读压损。"""
        ...


__all__ = [
    "BoundaryRoleCandidate",
    "CollectedOutputs",
    "EnvironmentProbe",
    "EvidenceMode",
    "ExecutionBudget",
    "JobHandle",
    "JobStatus",
    "LicenseStatus",
    "PreparedCase",
    "RawMetrics",
    "ReadbackEntry",
    "ReadbackSet",
    "StarAdapter",
    "TemplateInspection",
    "WhitelistWrite",
]
