"""FR-14/FR-15: evidence gaps cannot produce numerical PASS.

All numbers below are synthetic test inputs. They do not freeze an Owner's
engineering thresholds or claim real solver evidence.
"""
from __future__ import annotations

from copy import deepcopy

import pytest

from dsh_sim.canonical import canonical_dumps
from dsh_sim.verify.extract import ExtractedMetrics, extract_run_metrics
from dsh_sim.verify.verifier import verify_run

pytestmark = pytest.mark.mock

CONFIRMED_DOMAIN = {
    "applicability": {
        "geometry_scope": "CONFIRMED",
        "condition_scope": "CONFIRMED",
        "physics_scope": "CONFIRMED",
        "usage_scope": "CONFIRMED",
    }
}
SYNTHETIC_RULE = {
    "rule_id": "RULE-MASS-IMBALANCE",
    "target_metric": "steady_mass_imbalance",
    "threshold": 0.02,
}


def _verify(
    metrics: dict | None = None,
    *,
    rule: dict | None = None,
    required: list[str] | None = None,
    findings: list[dict] | None = None,
    missing: list[str] | None = None,
):
    return verify_run(
        extracted=ExtractedMetrics(
            metric_values={"steady_mass_imbalance": 0.01} if metrics is None else metrics,
            findings=findings or [],
            missing_metrics=missing or [],
        ),
        rules={"status": "RELEASED", "rules": [deepcopy(SYNTHETIC_RULE) if rule is None else rule]},
        domain=CONFIRMED_DOMAIN,
        required_metrics=required,
    )


def _kinds(result) -> set[str]:
    return {finding["kind"] for finding in result.findings}


def test_missing_required_metric_blocks_otherwise_passing_rule():
    result = _verify(required=["steady_mass_imbalance", "total_pressure_loss"])

    assert result.conclusion == "INSUFFICIENT"
    assert result.applicability == "IN_SCOPE"
    assert "REQUIRED_METRIC_MISSING" in _kinds(result)
    assert "total_pressure_loss" in result.check_inputs["missing_metrics"]


def test_required_ids_are_exact_and_do_not_guess_aliases_or_families():
    metrics = {"steady_mass_imbalance": 0.01, "boundary_mass_flow@inlet": -0.5}
    exact = _verify(metrics, required=["steady_mass_imbalance", "boundary_mass_flow@inlet"])
    guessed = _verify(metrics, required=["mass_imbalance", "boundary_mass_flow"])

    assert exact.conclusion == "PASS"
    assert guessed.conclusion == "INSUFFICIENT"
    assert set(guessed.check_inputs["missing_metrics"]) == {"mass_imbalance", "boundary_mass_flow"}


@pytest.mark.parametrize("value", [None, float("nan"), float("inf"), -float("inf"), True, "0.01"])
def test_invalid_required_value_is_missing_and_check_record_is_json_safe(value):
    metrics = {"steady_mass_imbalance": 0.01, "required_observation": value}
    extracted = ExtractedMetrics(metric_values=metrics)
    result = verify_run(
        extracted=extracted,
        rules={"rules": [SYNTHETIC_RULE]},
        domain=CONFIRMED_DOMAIN,
        required_metrics=["required_observation"],
    )

    assert result.conclusion == "INSUFFICIENT"
    assert result.check_inputs["metric_values"]["required_observation"] is None
    assert "REQUIRED_METRIC_MISSING" in _kinds(result)
    assert extracted.missing_metrics == []
    assert extracted.metric_values is metrics
    assert extracted.metric_values["required_observation"] is value
    canonical_dumps(result.check_inputs)  # NaN/Inf must not poison the frozen bundle.


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf"), -0.01])
def test_invalid_rule_input_never_passes(value):
    result = _verify({"steady_mass_imbalance": value})

    assert result.conclusion == "INSUFFICIENT"
    canonical_dumps(result.check_inputs)


def test_explicit_missing_flag_cannot_be_overridden_by_stale_numeric_value():
    result = _verify(missing=["steady_mass_imbalance"], required=["steady_mass_imbalance"])

    assert result.conclusion == "INSUFFICIENT"
    assert "RULE_INPUT_MISSING" in _kinds(result)


@pytest.mark.parametrize("threshold", [float("nan"), float("inf"), -float("inf"), True, "0.02", -0.01])
def test_invalid_threshold_never_passes_or_raises(threshold):
    result = _verify(rule={**SYNTHETIC_RULE, "threshold": threshold})

    assert result.conclusion == "INSUFFICIENT"
    assert "RULE_INVALID_THRESHOLD" in _kinds(result)


@pytest.mark.parametrize(
    "rule",
    [
        {"rule_id": "RULE-NEW", "target_metric": "new_metric", "threshold": 0.02},
        {"rule_id": "RULE-GRID-POLICY", "target_metric": None, "approved_grid_policy": "synthetic"},
        {**SYNTHETIC_RULE, "rule_id": "RULE-NEW"},
        {**SYNTHETIC_RULE, "check": "steady_mass_imbalance >= threshold"},
        {**SYNTHETIC_RULE, "operator": ">="},
        {**SYNTHETIC_RULE, "reference_floor": 0.01},
    ],
)
def test_unimplemented_or_changed_rule_cannot_silently_pass(rule):
    result = _verify({"steady_mass_imbalance": 0.01, "new_metric": 0.01}, rule=rule)

    assert result.conclusion == "INSUFFICIENT"
    assert "RULE_UNSUPPORTED" in _kinds(result)


def test_partly_frozen_rule_remains_unconfirmed():
    result = _verify(
        {"monitor_stability": 0.01},
        rule={
            "rule_id": "RULE-MONITOR-STABILITY",
            "target_metric": "monitor_stability",
            "window": None,
            "min_samples": "TBD",
            "allowed_variation": 0.02,
        },
    )

    assert result.conclusion == "INSUFFICIENT"
    assert "RULE_UNCONFIRMED" in _kinds(result)


@pytest.mark.parametrize("entries", [None, [], {}, [None], [SYNTHETIC_RULE, SYNTHETIC_RULE]])
def test_missing_malformed_or_ambiguous_rule_set_cannot_pass(entries):
    result = verify_run(
        extracted=ExtractedMetrics(metric_values={"steady_mass_imbalance": 0.01}),
        rules={"rules": entries},
        domain=CONFIRMED_DOMAIN,
    )

    assert result.conclusion == "INSUFFICIENT"


@pytest.mark.parametrize("kind", ["SIGN_CONVENTION", "BACKFLOW", "UNFROZEN_THRESHOLD", "MALFORMED_ROW"])
def test_extraction_problem_blocks_pass_even_with_finite_values(kind):
    finding = {"kind": kind, "message": "Synthetic extraction evidence gap"}
    result = _verify(findings=[finding])

    assert result.conclusion == "INSUFFICIENT"
    assert finding in result.findings


def test_known_violation_stays_fail_while_preserving_missing_evidence():
    result = _verify({"steady_mass_imbalance": 0.03}, required=["missing_observation"])

    assert result.conclusion == "FAIL"
    assert {"RULE_VIOLATED", "REQUIRED_METRIC_MISSING"} <= _kinds(result)


@pytest.mark.parametrize("static_pressure", ["100.0", "", "nan", "inf"])
def test_static_pressure_is_read_directly_and_never_replaced_with_total_pressure(tmp_path, static_pressure):
    report = tmp_path / "report.mock.csv"
    report.write_text(
        "section,boundary_role,sign_convention,mass_flow_kg_s,total_pressure_pa,static_pressure_pa\n"
        f"inlet,inlet,outward_positive,-0.5,150.0,{static_pressure}\n"
        "outlet,outlet,outward_positive,0.5,50.0,0.0\n",
        encoding="utf-8",
    )
    result = extract_run_metrics(report, None)

    assert result.metric_values["total_pressure@inlet"] == 150.0
    assert result.metric_values["static_pressure@outlet"] == 0.0
    if static_pressure == "100.0":
        assert result.metric_values["static_pressure@inlet"] == 100.0
        assert "static_pressure@inlet" not in result.missing_metrics
    else:
        assert result.metric_values["static_pressure@inlet"] is None
        assert "static_pressure@inlet" in result.missing_metrics
        assert any(f["kind"] == "MISSING_VALUE" for f in result.findings)


@pytest.mark.parametrize(
    "metric_id,parameter,value",
    [
        ("steady_mass_imbalance", "m_floor", "TBD"),
        ("steady_mass_imbalance", "m_floor", float("inf")),
        ("monitor_stability", "min_samples", "TBD"),
        ("monitor_stability", "min_samples", 0),
        ("monitor_stability", "min_samples", 1.5),
        ("monitor_stability", "window", float("inf")),
        ("monitor_stability", "window", "TBD"),
    ],
)
def test_invalid_definition_parameter_is_missing_instead_of_crashing_or_hiding_imbalance(
    tmp_path, metric_id, parameter, value
):
    report = tmp_path / "report.mock.csv"
    report.write_text(
        "section,boundary_role,sign_convention,mass_flow_kg_s,total_pressure_pa,static_pressure_pa\n"
        "inlet,inlet,outward_positive,-0.5,150.0,100.0\n"
        "outlet,outlet,outward_positive,0.4,50.0,0.0\n",
        encoding="utf-8",
    )
    monitor = tmp_path / "monitor.mock.csv"
    monitor.write_text("iteration,residual\n", encoding="utf-8")
    definitions = [
        {"metric_id": "steady_mass_imbalance", "m_floor": 0.01},
        {"metric_id": "monitor_stability", "window": 2, "min_samples": 2},
    ]
    next(item for item in definitions if item["metric_id"] == metric_id)[parameter] = value

    result = extract_run_metrics(report, monitor, metric_definitions={"metrics": definitions})

    assert result.metric_values[metric_id] is None
    assert metric_id in result.missing_metrics
    assert any(
        f.get("metric_id") == metric_id
        and f["kind"] in {"UNFROZEN_THRESHOLD", "INVALID_METRIC_DEFINITION"}
        for f in result.findings
    )
