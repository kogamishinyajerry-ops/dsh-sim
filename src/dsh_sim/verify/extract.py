"""确定性指标提取（FR-14；定义书 §指标定义 / §原始数据与派生数据分开）。

- 从原始报告/监控 CSV（只读 artifact）解析并复算派生指标；不照抄软件报告结论。
- 缺值 = None + missing_metrics 标记，绝不填 0、不估读、不由模型推测（诚实红线）。
- 工程阈值（m_floor / 监控窗口 / 批准截面 / 容差）一律不填：规则为 null/TBD 时
  对应指标值为 None 并生成 finding，交由 verifier 判 UNCONFIRMED（TBD-06）。
- 单位语义检查（FR-04）：表压无参考绝压、压力字段非 Pa 单位 → finding。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPORT_COLUMNS = (
    "section",
    "boundary_role",
    "sign_convention",
    "mass_flow_kg_s",
    "total_pressure_pa",
    "static_pressure_pa",
)


@dataclass(frozen=True)
class BoundarySample:
    section: str
    boundary_role: str
    sign_convention: str
    mass_flow_kg_s: float | None
    total_pressure_pa: float | None
    static_pressure_pa: float | None


@dataclass
class ExtractedMetrics:
    """提取结果：值 + 缺失 + findings（单位/口径问题逐条列出）。"""

    metric_values: dict[str, float | None]
    missing_metrics: list[str] = field(default_factory=list)
    findings: list[dict[str, Any]] = field(default_factory=list)
    boundary_samples: list[BoundarySample] = field(default_factory=list)


def _to_float(raw: str) -> float | None:
    """空串/非数值 → None（缺失）；NaN/Inf → None + 由调用方记 finding。"""
    raw = raw.strip()
    if raw == "":
        return None
    try:
        v = float(raw)
    except ValueError:
        return None
    if math.isnan(v) or math.isinf(v):
        return None
    return v


def parse_report_csv(path: str | Path) -> tuple[list[BoundarySample], list[dict[str, Any]]]:
    """解析固定列报告 CSV（# 开头为注释）。坏行不丢弃：记 finding，字段缺失为 None。"""
    samples: list[BoundarySample] = []
    findings: list[dict[str, Any]] = []
    path = Path(path)
    if not path.is_file():
        return [], [{"kind": "MISSING_FILE", "message": f"报告 CSV 不存在: {path}"}]
    header_seen = False
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        if text == ",".join(REPORT_COLUMNS):
            header_seen = True
            continue
        parts = text.split(",")
        if len(parts) != len(REPORT_COLUMNS):
            findings.append(
                {"kind": "MALFORMED_ROW", "message": f"第 {lineno} 行列数不符: {text!r}"}
            )
            continue
        section, role, sign, m, tp, sp = parts
        sample = BoundarySample(
            section=section.strip(),
            boundary_role=role.strip(),
            sign_convention=sign.strip(),
            mass_flow_kg_s=_to_float(m),
            total_pressure_pa=_to_float(tp),
            static_pressure_pa=_to_float(sp),
        )
        for name, val in (
            ("mass_flow_kg_s", sample.mass_flow_kg_s),
            ("total_pressure_pa", sample.total_pressure_pa),
        ):
            if val is None:
                findings.append(
                    {
                        "kind": "MISSING_VALUE",
                        "message": f"第 {lineno} 行 {sample.boundary_role} 缺 {name}（标缺失，不填 0）",
                    }
                )
        samples.append(sample)
    if not header_seen and samples:
        findings.append({"kind": "NO_HEADER", "message": "报告 CSV 缺少约定表头"})
    return samples, findings


def extract_run_metrics(
    report_csv: str | Path | None,
    monitor_csv: str | Path | None,
    *,
    metric_definitions: dict[str, Any] | None = None,
) -> ExtractedMetrics:
    """从原始 artifact 独立复算一个 Run 的指标（定义书 §指标定义 六个指标框架）。

    metric_definitions：能力包 metric-definitions.json 内容；其中的 null/TBD
    阈值（m_floor/window/min_samples/approved_sections）决定对应指标能否取值。
    """
    out = ExtractedMetrics(metric_values={})
    defs = {m["metric_id"]: m for m in (metric_definitions or {}).get("metrics", [])}

    if report_csv is None:
        out.missing_metrics.append("boundary_mass_flow")
        # 派生指标一并标缺失（键存在、值为 None），绝不填 0
        for key in ("steady_mass_imbalance", "total_pressure_loss"):
            out.metric_values[key] = None
            out.missing_metrics.append(key)
        out.findings.append(
            {"kind": "MISSING_ARTIFACT", "message": "无报告 CSV artifact，边界量标缺失"}
        )
    else:
        samples, findings = parse_report_csv(report_csv)
        out.boundary_samples = samples
        out.findings.extend(findings)

        inlets = [s for s in samples if s.section == "inlet"]
        outlets = [s for s in samples if s.section == "outlet"]

        # 边界质量流量/总压（原始量，含符号约定记录）
        for s in samples:
            out.metric_values[f"boundary_mass_flow@{s.boundary_role}"] = s.mass_flow_kg_s
            out.metric_values[f"total_pressure@{s.boundary_role}"] = s.total_pressure_pa
            if s.mass_flow_kg_s is None:
                out.missing_metrics.append(f"boundary_mass_flow@{s.boundary_role}")
            if s.total_pressure_pa is None:
                out.missing_metrics.append(f"total_pressure@{s.boundary_role}")
            if s.sign_convention != "outward_positive":
                out.findings.append(
                    {
                        "kind": "SIGN_CONVENTION",
                        "message": f"{s.boundary_role} 符号约定为 {s.sign_convention!r}，"
                        "与 outward_positive 约定不一致，需转换记录",
                    }
                )

        # 稳态质量不平衡：abs(sum m_b) / max(sum_in abs(m_b), m_floor)
        m_floor = (defs.get("steady_mass_imbalance") or {}).get("m_floor")
        flows = [s.mass_flow_kg_s for s in samples]
        if any(f is None for f in flows) or not flows:
            out.metric_values["steady_mass_imbalance"] = None
            out.missing_metrics.append("steady_mass_imbalance")
        elif m_floor is None:
            # m_floor 是 Owner 冻结保护尺度；未冻结不得自编分母（TBD-06）
            out.metric_values["steady_mass_imbalance"] = None
            out.missing_metrics.append("steady_mass_imbalance")
            out.findings.append(
                {
                    "kind": "UNFROZEN_THRESHOLD",
                    "metric_id": "steady_mass_imbalance",
                    "message": "m_floor 未冻结（Owner/TBD-06），稳态质量不平衡无法按批准口径计算",
                }
            )
        else:
            sum_in = sum(abs(f) for f in flows if f < 0)  # type: ignore[operator]
            denom = max(sum_in, float(m_floor))
            if denom <= 0:
                out.metric_values["steady_mass_imbalance"] = None
                out.findings.append(
                    {
                        "kind": "NOT_APPLICABLE",
                        "metric_id": "steady_mass_imbalance",
                        "message": "接近零流量，质量不平衡指标不适用（不靠分母掩盖错误）",
                    }
                )
            else:
                out.metric_values["steady_mass_imbalance"] = abs(sum(flows)) / denom  # type: ignore[arg-type]

        # 出口分配：f_i = m_i / sum_out m_i（正向出流范围）
        out_flows = [s for s in outlets if s.mass_flow_kg_s is not None and s.mass_flow_kg_s > 0]
        sum_out = sum(s.mass_flow_kg_s for s in out_flows)  # type: ignore[arg-type]
        backflow = [s for s in outlets if s.mass_flow_kg_s is not None and s.mass_flow_kg_s < 0]
        if backflow:
            out.findings.append(
                {
                    "kind": "BACKFLOW",
                    "metric_id": "outlet_distribution",
                    "message": "存在出口回流：禁止套用简单正向权重，分配指标转不适用/专门规则",
                }
            )
        for s in outlets:
            key = f"outlet_distribution@{s.boundary_role}"
            if sum_out <= 0 or s.mass_flow_kg_s is None or s.mass_flow_kg_s <= 0:
                out.metric_values[key] = None
                out.missing_metrics.append(key)
            else:
                out.metric_values[key] = s.mass_flow_kg_s / sum_out

        # 总压损失：入口/出口质量流量加权总压差（截面须经批准）
        approved_sections = (defs.get("total_pressure_loss") or {}).get("approved_sections")
        tp_in = [s for s in inlets if s.total_pressure_pa is not None and s.mass_flow_kg_s]
        tp_out = [s for s in out_flows if s.total_pressure_pa is not None]
        if not tp_in or not tp_out:
            out.metric_values["total_pressure_loss"] = None
            out.missing_metrics.append("total_pressure_loss")
        else:
            w_in = sum(abs(s.mass_flow_kg_s) for s in tp_in)  # type: ignore[arg-type]
            w_out = sum(s.mass_flow_kg_s for s in tp_out)  # type: ignore[arg-type]
            pin = sum(s.total_pressure_pa * abs(s.mass_flow_kg_s) for s in tp_in) / w_in  # type: ignore[operator]
            pout = sum(s.total_pressure_pa * s.mass_flow_kg_s for s in tp_out) / w_out  # type: ignore[operator]
            out.metric_values["total_pressure_loss"] = pin - pout
            if approved_sections is None:
                out.findings.append(
                    {
                        "kind": "UNFROZEN_THRESHOLD",
                        "metric_id": "total_pressure_loss",
                        "message": "批准截面/权重未冻结（Owner/TBD-06），总压损失口径 UNCONFIRMED",
                    }
                )

    # 监控稳定性：窗口/最小采样长度由规则包给定（null → 不取值）
    stab = defs.get("monitor_stability") or {}
    if stab.get("window") is None or stab.get("min_samples") is None:
        out.metric_values["monitor_stability"] = None
        out.missing_metrics.append("monitor_stability")
        out.findings.append(
            {
                "kind": "UNFROZEN_THRESHOLD",
                "metric_id": "monitor_stability",
                "message": "监控窗口/最小采样长度未冻结（Owner/TBD-06），监控稳定性无法按批准口径判定",
            }
        )
    elif monitor_csv is None:
        out.metric_values["monitor_stability"] = None
        out.missing_metrics.append("monitor_stability")
        out.findings.append(
            {"kind": "MISSING_ARTIFACT", "message": "无监控 CSV artifact，监控稳定性标缺失"}
        )
    else:
        # 窗口/采样长度已冻结（测试或 Owner 冻结后）：取首监控列全样本 max-min 作为
        # 观测变幅；是否满足允许值由 verifier 按 allowed_variation 判定。
        series: list[float] = []
        for line in Path(monitor_csv).read_text(encoding="utf-8").splitlines():
            text = line.strip()
            if not text or text.startswith("#") or text.startswith("iteration"):
                continue
            parts = text.split(",")
            if len(parts) >= 2:
                v = _to_float(parts[1])
                if v is not None:
                    series.append(v)
        min_samples = int(stab["min_samples"])
        if len(series) < min_samples:
            out.metric_values["monitor_stability"] = None
            out.missing_metrics.append("monitor_stability")
            out.findings.append(
                {
                    "kind": "INSUFFICIENT_SAMPLES",
                    "metric_id": "monitor_stability",
                    "message": f"监控样本 {len(series)} < 最小采样长度 {min_samples}，标缺失",
                }
            )
        else:
            out.metric_values["monitor_stability"] = max(series) - min(series)
    return out


def check_unit_semantics(spec: dict[str, Any]) -> list[dict[str, Any]]:
    """单位与压力语义检查（FR-04）：表压无参考绝压 / 压力字段非 Pa → finding。"""
    findings: list[dict[str, Any]] = []
    for cond in spec.get("conditions", []):
        cid = cond.get("condition_id", "?")
        for f in cond.get("fields", []):
            q = f.get("quantity") or {}
            field_name = f.get("field", "?")
            if "pressure" in field_name and q.get("unit") != "Pa":
                findings.append(
                    {
                        "kind": "UNIT_MISMATCH",
                        "message": f"工况 {cid} 字段 {field_name} 单位为 {q.get('unit')!r}，压力字段须为 Pa",
                    }
                )
            if q.get("pressure_kind") == "gauge" and not q.get("reference_absolute_pressure"):
                findings.append(
                    {
                        "kind": "GAUGE_WITHOUT_REFERENCE",
                        "message": f"工况 {cid} 字段 {field_name} 为表压但无参考绝压（不得进入准备）",
                    }
                )
    return findings


__all__ = [
    "BoundarySample",
    "ExtractedMetrics",
    "REPORT_COLUMNS",
    "check_unit_semantics",
    "extract_run_metrics",
    "parse_report_csv",
]
