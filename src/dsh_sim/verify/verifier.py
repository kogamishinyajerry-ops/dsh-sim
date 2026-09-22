"""独立数值校核（FR-15/FR-16；定义书 §数值、方法与工程接受的门 G-numerical）。

- 独立复算：输入为 verify/extract.py 从原始 artifact 提取的值，不照抄软件报告。
- 程序成功与数值通过分别判定：conclusion 只来自本模块的规则评估。
- 规则来自能力包 rules.json：**阈值 null/TBD → 该项 UNCONFIRMED**，
  finding 写明"Owner 未冻结（TBD-06）"；绝不替 Owner 编一个数（诚实红线 §0.3）。
- 结论规则：任一已冻结规则被违反 → FAIL；存在缺值/UNCONFIRMED 项 → INSUFFICIENT
  （无法诚实判 PASS）；全部规则已冻结且通过 → PASS。
- 适用性（FR-16）：domain.json 适用范围任一项非 CONFIRMED → UNCONFIRMED。
"""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from dsh_sim.canonical import canonical_dumps, sha256_hex
from dsh_sim.db.models import RunRow, VerificationRow
from dsh_sim.domain.states import ApplicabilityState, NumericalState, can_transition
from dsh_sim.verify.extract import ExtractedMetrics

ENV_CAPABILITIES_ROOT = "DSH_SIM_CAPABILITIES_ROOT"
DEFAULT_CAPABILITIES_ROOT = Path(__file__).resolve().parents[3] / "capabilities"

# rules.json 中会被视为"阈值"的字段（任一非 None 即视为已冻结可判定）
_THRESHOLD_KEYS = (
    "threshold",
    "window",
    "min_samples",
    "allowed_variation",
    "reference_floor",
    "approved_grid_policy",
)


@dataclass
class VerificationResult:
    conclusion: str  # PASS / FAIL / INSUFFICIENT
    findings: list[dict[str, Any]] = field(default_factory=list)
    check_inputs: dict[str, Any] = field(default_factory=dict)
    applicability: str = "UNCONFIRMED"


def capabilities_root() -> Path:
    return Path(os.environ.get(ENV_CAPABILITIES_ROOT) or DEFAULT_CAPABILITIES_ROOT)


def load_rule_set(package_id: str, version: str) -> tuple[dict[str, Any], str]:
    """加载能力包 rules.json；返回 (rules_dict, rule_set_sha256)。规则集随检查记录摘要。"""
    path = capabilities_root() / package_id / version / "rules.json"
    if not path.is_file():
        raise FileNotFoundError(f"规则集不存在: {path}")
    text = path.read_text(encoding="utf-8")
    return json.loads(text), sha256_hex(canonical_dumps(json.loads(text)))


def load_metric_definitions(package_id: str, version: str) -> dict[str, Any]:
    path = capabilities_root() / package_id / version / "metric-definitions.json"
    if not path.is_file():
        raise FileNotFoundError(f"指标定义不存在: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def load_domain(package_id: str, version: str) -> dict[str, Any]:
    path = capabilities_root() / package_id / version / "domain.json"
    if not path.is_file():
        raise FileNotFoundError(f"能力包 domain 不存在: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def assess_applicability(domain: dict[str, Any]) -> str:
    """FR-16：范围未知统一 UNCONFIRMED；任何一项 UNCONFIRMED 不得显示为可用结论。"""
    scope = (domain.get("applicability") or {})
    values = [
        scope.get("geometry_scope"),
        scope.get("condition_scope"),
        scope.get("physics_scope"),
        scope.get("usage_scope"),
    ]
    if all(v == "CONFIRMED" for v in values):
        return ApplicabilityState.IN_SCOPE.value
    return ApplicabilityState.UNCONFIRMED.value


def _rule_thresholds(rule: dict[str, Any]) -> dict[str, Any]:
    return {k: rule[k] for k in _THRESHOLD_KEYS if k in rule}


def verify_run(
    *,
    extracted: ExtractedMetrics,
    rules: dict[str, Any],
    domain: dict[str, Any],
    required_metrics: list[str] | None = None,
) -> VerificationResult:
    """对一个 Run 的提取结果做独立校核。

    规则阈值全部 null/TBD（当前 buffer_chamber/0.1.0 骨架状态）时：
    每条规则产生 UNCONFIRMED finding，结论不可能是 PASS（诚实，不编阈值）。
    """
    findings: list[dict[str, Any]] = list(extracted.findings)
    has_fail = False
    has_gap = bool(extracted.missing_metrics)

    for mid in extracted.missing_metrics:
        findings.append(
            {
                "kind": "MISSING_METRIC",
                "metric_id": mid,
                "message": f"指标 {mid} 缺失（null + MISSING；不填 0 不估算）",
            }
        )

    for rule in rules.get("rules", []):
        rid = rule.get("rule_id", "?")
        target = rule.get("target_metric")
        thresholds = _rule_thresholds(rule)
        frozen = {k: v for k, v in thresholds.items() if v is not None and v != "TBD"}
        if thresholds and not frozen:
            findings.append(
                {
                    "kind": "RULE_UNCONFIRMED",
                    "rule_id": rid,
                    "severity": rule.get("severity"),
                    "message": f"规则 {rid} 阈值 null/TBD：Owner 未冻结（TBD-06），本项 UNCONFIRMED，不判定",
                }
            )
            has_gap = True
            continue
        if not thresholds:
            # 无阈值字段的规则（如适用域门）由 assess_applicability 另行处理
            findings.append(
                {
                    "kind": "RULE_UNCONFIRMED",
                    "rule_id": rid,
                    "severity": rule.get("severity"),
                    "message": f"规则 {rid} 无已冻结判据（Owner 未冻结），本项 UNCONFIRMED",
                }
            )
            has_gap = True
            continue
        # 已冻结阈值的确定性判定（当前骨架无此分支；阈值冻结后在此扩展逐规则复算）
        value = extracted.metric_values.get(target) if target else None
        if target and value is None:
            findings.append(
                {
                    "kind": "RULE_INPUT_MISSING",
                    "rule_id": rid,
                    "message": f"规则 {rid} 目标指标 {target} 缺失，无法判定",
                }
            )
            has_gap = True
        elif target == "steady_mass_imbalance" and "threshold" in frozen:
            if value > float(frozen["threshold"]):
                findings.append(
                    {
                        "kind": "RULE_VIOLATED",
                        "rule_id": rid,
                        "message": f"质量不平衡 {value} 超过阈值 {frozen['threshold']}",
                    }
                )
                has_fail = True

    applicability = assess_applicability(domain)
    if applicability != ApplicabilityState.IN_SCOPE.value:
        findings.append(
            {
                "kind": "APPLICABILITY_UNCONFIRMED",
                "message": "能力包适用范围存在 UNCONFIRMED 项（TBD-04），适用性不能显示为 IN_SCOPE",
            }
        )

    if has_fail:
        conclusion = NumericalState.FAIL.value
    elif has_gap:
        conclusion = NumericalState.INSUFFICIENT.value
    else:
        conclusion = NumericalState.PASS.value

    return VerificationResult(
        conclusion=conclusion,
        findings=findings,
        check_inputs={
            "metric_values": extracted.metric_values,
            "missing_metrics": extracted.missing_metrics,
            "required_metrics": required_metrics or [],
            "rule_status": rules.get("status"),
        },
        applicability=applicability,
    )


def persist_verification(
    session: Session,
    *,
    run: RunRow,
    attempt_id: str | None,
    result: VerificationResult,
    rule_set_sha256: str,
    source_artifact_ids: list[str],
) -> VerificationRow:
    """写入 VerificationRow 并迁移 Run 数值/适用性状态。

    重新检查产生新 Verification 记录（旧记录不原位改写，定义书 §修订）。
    Run 状态只按状态机合法迁移更新；已定格的数值状态不被覆写。
    """
    row = VerificationRow(
        verification_id=f"ver_{uuid.uuid4().hex[:24]}",
        run_id=run.run_id,
        attempt_id=attempt_id,
        rule_set_sha256=rule_set_sha256,
        check_inputs=result.check_inputs,
        conclusion=result.conclusion,
        findings=result.findings,
        source_artifact_ids=source_artifact_ids,
    )
    session.add(row)

    current_num = NumericalState(run.numerical_state)
    target_num = NumericalState(result.conclusion)
    if can_transition(current_num, target_num):
        run.numerical_state = target_num.value

    current_app = ApplicabilityState(run.applicability_state)
    target_app = ApplicabilityState(result.applicability)
    if current_app != target_app and can_transition(current_app, target_app):
        run.applicability_state = target_app.value

    session.flush()
    return row


__all__ = [
    "VerificationResult",
    "assess_applicability",
    "capabilities_root",
    "load_domain",
    "load_metric_definitions",
    "load_rule_set",
    "persist_verification",
    "verify_run",
]
