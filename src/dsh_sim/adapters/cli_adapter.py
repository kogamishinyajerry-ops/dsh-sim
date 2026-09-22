"""StarCliAdapter —— 真实 STAR-CCM+ 支（薄封装 <STARCCM_CLI_DIR>\\starccm_cli.py）。

约束（compatibility/probe-report.md §对本机 CLI 桥的重要实测发现）：
- subprocess 走 args list，禁 shell=True；禁止 shell 拼接命令。
- spawn 语法 [bat, sim, "-batch", macro]（sim 在前），-batch 后拒绝额外位置参数
  → 宏参数必须烘焙进 .java 源码；spawn 前设 JAVA_TOOL_OPTIONS=-Dfile.encoding=UTF-8。
- 桥 `checkpoint` 命令仍用旧参数顺序（实测 rc=2），本适配器不复用该命令。

实现状态（与 P01–P09 探针结论一致，诚实红线 §0.2/§0.4）：
- probe_environment：已实现（capabilities 真实命令成功时 evidence_mode="REAL"）。
- inspect_template：已实现（inspect-sim 静态解析，不经求解器 spawn）；
  边界角色候选为空并在 class_name_guess_warning 声明 P04 NOT_RUN。
- prepare_case / read_actual_settings / launch / poll / cancel /
  collect_outputs / extract_metrics：raise NotImplementedError("NOT_RUN: <解锁条件>")，
  解锁条件逐条引用探针报告与 TBD 编号，绝不补造能力。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from dsh_sim.adapters.star_adapter import (
    CollectedOutputs,
    EnvironmentProbe,
    ExecutionBudget,
    JobHandle,
    JobStatus,
    LicenseStatus,
    PreparedCase,
    RawMetrics,
    ReadbackSet,
    TemplateInspection,
    WhitelistWrite,
)

DEFAULT_CLI_PATH = r"<STARCCM_CLI_DIR>\starccm_cli.py"
ENV_CLI_PATH = "DSH_SIM_STAR_CLI"
ENV_CLI_PYTHON = "DSH_SIM_STAR_PYTHON"
DEFAULT_TIMEOUT_SECONDS = 120


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class StarCliAdapter:
    """真实支薄封装。只实现有探针证据支撑的接口。"""

    adapter_build = "star-cli-adapter/0.1.0 (bridge v49.0.0)"

    def __init__(
        self,
        *,
        cli_path: str | None = None,
        python_exe: str | None = None,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.cli_path = cli_path or os.environ.get(ENV_CLI_PATH, DEFAULT_CLI_PATH)
        self.python_exe = python_exe or os.environ.get(ENV_CLI_PYTHON, sys.executable)
        self.timeout_seconds = timeout_seconds

    # ------------------------------------------------------------------
    # 内部：受控子进程（args list，禁 shell=True）
    # ------------------------------------------------------------------

    def _run_cli(self, args: list[str]) -> dict:
        """调用 CLI 桥并解析 v3 JSON payload。真实命令失败如实抛出。"""
        env = os.environ.copy()
        # spawn STAR-CCM+ 前必须 UTF-8（桥 v34 注释；中文 Windows javac GBK 会炸中文宏）
        env["JAVA_TOOL_OPTIONS"] = "-Dfile.encoding=UTF-8"
        env["JAVAC_OPTIONS"] = "-encoding UTF-8"
        argv = [self.python_exe, self.cli_path, *args, "--json"]
        proc = subprocess.run(
            argv,  # args list；禁止 shell 拼接
            shell=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=self.timeout_seconds,
        )
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"CLI 桥输出非 v3 JSON（rc={proc.returncode}）：{proc.stdout[:300]!r} "
                f"stderr={proc.stderr[:300]!r}"
            ) from exc
        if proc.returncode != 0 or not payload.get("ok"):
            raise RuntimeError(
                f"CLI 桥命令失败（{args!r}, rc={proc.returncode}）："
                f"{payload.get('error') or payload!r}"
            )
        return payload

    # ------------------------------------------------------------------
    # 已实现：环境探针（P01 PASS 限定——桥可用；授权状态不猜）
    # ------------------------------------------------------------------

    def probe_environment(self) -> EnvironmentProbe:
        """读取真实构建号/可执行路径/版本特性/授权状态；无合法授权不得继续。

        - capabilities 真实命令成功 → evidence_mode="REAL"；失败则异常上抛，
          绝不返回伪造 REAL 探针（诚实红线 §0.1/§0.2）。
        - star_build / 授权：本桥 capabilities 不直接返回求解器构建号与 license，
          如实标 UNCONFIRMED；真实值以 compatibility/P01 探针记录为准。
        """
        payload = self._run_cli(["capabilities"])
        data = payload["data"]
        commands = tuple(sorted(c["name"] for c in data.get("commands", [])))
        return EnvironmentProbe(
            star_build=(
                "UNCONFIRMED: capabilities 实跑成功（桥 v"
                + str(data.get("version", "?"))
                + "）；求解器真实构建号以 compatibility/P01-version-license.json 为准"
            ),
            executable_path=(
                "UNCONFIRMED: 主用路径见 P01 探针记录 "
                "(C:\\Program Files\\Siemens\\19.02.009-R8\\...\\starccm+.bat)"
            ),
            version_features=commands,
            license_status=LicenseStatus.UNCONFIRMED,
            license_details={
                "note": "桥 capabilities 不含授权探测；P01 实测 available=[read_only,basic_geom,"
                "ccmpsuite_solve], missing=[ccmpsuite_init]（详见探针记录）；"
                "license 合规确认归 TBD-07",
            },
            probed_at=_utcnow_iso(),
            evidence_mode="REAL",
            evidence_refs=("compatibility/raw/p01_capabilities.json",),
        )

    # ------------------------------------------------------------------
    # 已实现（限定）：模板静态检查（inspect-sim 不经 spawn；P04 NOT_RUN 声明）
    # ------------------------------------------------------------------

    def inspect_template(self, template_ref: str) -> TemplateInspection:
        """inspect-sim 静态解析 .sim（v7：直接读文件，不依赖 spawn）。

        template_ref 必须是本机真实 .sim 路径（受控模板登记后由服务侧解析传入）。
        边界角色候选：静态解析不产出经人工标识一致的角色绑定（P04 NOT_RUN，
        TBD-05 缺批准 boundary-map）——返回空候选并在告警字段声明，
        不把猜测类名当作能力（定义书 §STAR-CCM+适配器定义）。
        """
        path = Path(template_ref)
        if not path.is_file():
            raise FileNotFoundError(f"模板文件不存在（不接受任意路径猜测）: {template_ref}")
        payload = self._run_cli(["inspect-sim", str(path)])
        data = payload.get("data") or {}
        # 静态解析结果原样摘要；分类启发式属桥实现，角色绑定不做推断
        summary = data if isinstance(data, dict) else {"raw": str(data)[:200]}
        return TemplateInspection(
            template_ref=template_ref,
            boundary_role_candidates=(),
            regions=(),
            physics_models=(),
            mesh_summary={"static_inspect": summary},
            report_definitions=(),
            class_name_guess_warning=(
                "P04 NOT_RUN：缺批准 boundary-map（TBD-05），边界角色/面积/质心签名未经"
                "人工标识一致确认；本输出仅为静态结构摘要，任何条目不得当作已确认能力。"
                "解锁条件：R4/Owner 交付批准 boundary-map.json + WP-06 录宏确认签名 API。"
            ),
            evidence_mode="REAL",
        )

    # ------------------------------------------------------------------
    # 未实现接口：按探针结论如实标注 NOT_RUN + 解锁条件
    # ------------------------------------------------------------------

    def prepare_case(
        self,
        template_ref: str,
        whitelist_writes: list[WhitelistWrite],
        work_dir: str,
    ) -> PreparedCase:
        raise NotImplementedError(
            "NOT_RUN: 固定写入宏未录制/登记（WP-06；ADR-04 运行时不自由生成宏）。"
            "P02 打开/保存与 P03 写入/回读仅证明手工路径可行；宏参数须烘焙进 .java 源码。"
            "解锁条件：WP-06 交付固定宏资产 + 批准模板登记 + 回写宏真实回读证据。"
        )

    def read_actual_settings(self, prepared_ref: str) -> ReadbackSet:
        raise NotImplementedError(
            "NOT_RUN: 固定回读宏未交付（WP-06）。P03 仅证明单字段写入/回读可行；"
            "完整 ReadbackSet（含模型身份）需录制回读宏并实测。"
            "解锁条件：WP-06 回读宏 + P03 扩展探针（全白名单字段）PASS。"
        )

    def launch(self, prepared_ref: str, budget: ExecutionBudget) -> JobHandle:
        raise NotImplementedError(
            "NOT_RUN: ccmpsuite_init 缺失（P01 实测）→ 全新算例初始化不可用；"
            "P05 仅限定『已初始化案例续算』。受控启动须走 v34 spawn 语法 "
            "[bat, sim, -batch, macro] + 参数烘焙，且预算执行（迭代上限/墙钟）宏未录制。"
            "解锁条件：TBD-07 授权边界决策（补 init license 或模板预初始化）+ WP-07 进程控制。"
        )

    def poll(self, job_handle: JobHandle) -> JobStatus:
        raise NotImplementedError(
            "NOT_RUN: 无受控 launch 即无可 poll 的真实句柄；外部进程状态采集"
            "（阶段/日志位置/资源状态）须随 WP-07 Worker 进程控制一并实测。"
            "解锁条件：launch 解锁 + P08 断线重入探针（依赖 Worker 交付）。"
        )

    def cancel(self, job_handle: JobHandle) -> JobStatus:
        raise NotImplementedError(
            "NOT_RUN: P07 仅限定通过（串行 batch taskkill /T 全终止）；"
            "MPI（-np>1）进程组与优雅停止（stop file）两级取消未补测。"
            "解锁条件：P07 完整版补测 PASS（Worker 实现期）；未过不得发布无人值守可取消节点。"
        )

    def collect_outputs(self, job_handle: JobHandle) -> CollectedOutputs:
        raise NotImplementedError(
            "NOT_RUN: 受控执行链未建立（launch 未解锁），无真实 attempt 产物可收集；"
            "P05 限定证明残差 CSV 可导出，但固定导出宏未录制（WP-06）。"
            "解锁条件：launch/poll 解锁 + WP-06 导出宏资产。"
        )

    def extract_metrics(self, collected: CollectedOutputs) -> RawMetrics:
        raise NotImplementedError(
            "NOT_RUN: 固定提取器依赖 collect_outputs 的真实产物集合；"
            "且 P06 重复性 NOT_RUN（Owner 未冻结重复性容差，TBD-06），提取口径未冻结。"
            "解锁条件：collect_outputs 解锁 + Owner 冻结指标口径（TBD-06）。"
        )


__all__ = ["StarCliAdapter"]
