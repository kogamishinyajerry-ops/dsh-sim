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
import math
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

# 声明的阈值必须全部冻结；仅填写其中一个字段不能使整条规则成为可判定规则。
_THRESHOLD_KEYS = (
    "threshold",
    "window",
    "min_samples",
    "allowed_variation",
    "reference_floor",
    "approved_grid_policy",
)
_MASS_IMBALANCE_CHECK = "abs(sum_b m_b) / max(sum_in abs(m_b), m_floor) <= threshold"


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


def _finite_number(value: Any) -> bool:
    """只接受提取器提供的有限 JSON 数字，拒绝 bool 和可转换的字符串。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def verify_run(
    *,
    extracted: ExtractedMetrics,
    rules: dict[str, Any],
    domain: dict[str, Any],
    required_metrics: list[str] | None = None,
) -> VerificationResult:
    """对一个 Run 的提取结果做独立校核。

    required_metrics 使用精确 metric_id，不猜测别名或边界指标族。
    null/TBD、无实现的规则、缺值及提取口径问题都会阻止 PASS；已确认违反
    受支持规则时仍返回 FAIL，并保留所有证据缺口。数值结论与适用性分别记录。
    """
    findings: list[dict[str, Any]] = list(extracted.findings)
    has_fail = False
    # 提取器 findings 当前均为缺失/单位/符号/未批准口径等问题，不能只展示而忽略。
    has_gap = bool(extracted.findings)
    metric_values = dict(extracted.metric_values)
    missing_metrics = list(dict.fromkeys(extracted.missing_metrics))
    required = list(required_metrics or [])

    for mid, value in metric_values.items():
        if value is not None and not _finite_number(value):
            findings.append(
                {
                    "kind": "INVALID_METRIC_VALUE",
                    "metric_id": mid,
                    "observed_value": repr(value),
                    "message": f"指标 {mid} 不是有限数值，保留问题并按缺失处理",
                }
            )
            # JSON/canonical 摘要不允许 NaN/Inf；不修改原始提取结果，也不填 0。
            metric_values[mid] = None
        if metric_values[mid] is None and mid not in missing_metrics:
            missing_metrics.append(mid)

    for mid in required:
        if mid in missing_metrics or metric_values.get(mid) is None:
            findings.append(
                {
                    "kind": "REQUIRED_METRIC_MISSING",
                    "metric_id": mid,
                    "message": f"任务要求指标 {mid}，但没有对应的有效提取值，无法判 PASS",
                }
            )
            if mid not in missing_metrics:
                missing_metrics.append(mid)

    for mid in missing_metrics:
        findings.append(
            {
                "kind": "MISSING_METRIC",
                "metric_id": mid,
                "message": f"指标 {mid} 缺失（null + MISSING；不填 0 不估算）",
            }
        )
    has_gap = has_gap or bool(missing_metrics)

    rule_entries = rules.get("rules")
    if not isinstance(rule_entries, list) or not rule_entries:
        findings.append(
            {"kind": "RULE_SET_INVALID", "message": "规则集缺失、为空或格式不符，无法判 PASS"}
        )
        has_gap = True
        rule_entries = []

    seen_rule_ids: set[str] = set()
    for rule in rule_entries:
        if not isinstance(rule, dict):
            findings.append(
                {"kind": "RULE_INVALID", "message": "规则条目不是对象，无法判定"}
            )
            has_gap = True
            continue
        rid = rule.get("rule_id", "?")
        target = rule.get("target_metric")
        if not isinstance(rid, str) or not rid or rid in seen_rule_ids:
            findings.append(
                {"kind": "RULE_INVALID", "message": "规则 ID 缺失、格式错误或重复，无法判定"}
            )
            has_gap = True
            continue
        seen_rule_ids.add(rid)
        thresholds = _rule_thresholds(rule)
        if not thresholds or any(v is None or v == "TBD" for v in thresholds.values()):
            findings.append(
                {
                    "kind": "RULE_UNCONFIRMED",
                    "rule_id": rid,
                    "severity": rule.get("severity"),
                    "message": f"规则 {rid} 判据缺失或含 null/TBD：Owner 未冻结（TBD-06），本项 UNCONFIRMED，不判定",
                }
            )
            has_gap = True
            continue

        # 已实现的比较器必须匹配规则身份、输入和判据形状。未知规则及变更后的
        # 自由文本不能因为带有 threshold 就被静默视为通过，也不执行文本表达式。
        if not (
            rid == "RULE-MASS-IMBALANCE"
            and target == "steady_mass_imbalance"
            and set(thresholds) == {"threshold"}
            and rule.get("check", _MASS_IMBALANCE_CHECK) == _MASS_IMBALANCE_CHECK
            and "operator" not in rule
            and "comparator" not in rule
        ):
            findings.append(
                {
                    "kind": "RULE_UNSUPPORTED",
                    "rule_id": rid,
                    "severity": rule.get("severity"),
                    "message": f"规则 {rid} 的比较器尚未实现或判据不匹配，无法判定",
                }
            )
            has_gap = True
            continue

        threshold = thresholds["threshold"]
        if not _finite_number(threshold) or threshold < 0:
            findings.append(
                {
                    "kind": "RULE_INVALID_THRESHOLD",
                    "rule_id": rid,
                    "observed_value": repr(threshold),
                    "message": f"规则 {rid} 需要有限且非负的质量不平衡阈值，无法判定",
                }
            )
            has_gap = True
            continue

        value = metric_values.get(target)
        if value is None or target in missing_metrics:
            findings.append(
                {
                    "kind": "RULE_INPUT_MISSING",
                    "rule_id": rid,
                    "message": f"规则 {rid} 目标指标 {target} 缺失，无法判定",
                }
            )
            has_gap = True
        elif value < 0:
            findings.append(
                {
                    "kind": "RULE_INPUT_INVALID",
                    "rule_id": rid,
                    "message": f"规则 {rid} 的质量不平衡输入为负数，无法判定",
                }
            )
            has_gap = True
        elif value > threshold:
            findings.append(
                {
                    "kind": "RULE_VIOLATED",
                    "rule_id": rid,
                    "message": f"质量不平衡 {value} 超过阈值 {threshold}",
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
            "metric_values": metric_values,
            "missing_metrics": missing_metrics,
            "required_metrics": required,
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
