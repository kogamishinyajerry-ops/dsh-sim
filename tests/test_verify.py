"""指标提取与独立校核（FR-14/15/16；WP-09）。

- 缺值 = null + MISSING，绝不填 0 或估算；
- 规则阈值 null/TBD → 该项 UNCONFIRMED（finding 写明 Owner 未冻结），不编数；
- 程序成功与数值通过分离；PASS/FAIL 只在阈值已冻结时判定（测试用合成冻结阈值）。
"""
from __future__ import annotations

import pytest

from dsh_sim.verify.extract import (
    check_unit_semantics,
    extract_run_metrics,
    parse_report_csv,
)
from dsh_sim.verify.verifier import (
    assess_applicability,
    load_domain,
    load_metric_definitions,
    load_rule_set,
    verify_run,
)

pytestmark = pytest.mark.mock

HEADER = "section,boundary_role,sign_convention,mass_flow_kg_s,total_pressure_pa,static_pressure_pa"
BALANCED = "\n".join(
    [
        "# test fixture csv",
        HEADER,
        "inlet,inlet,outward_positive,-1.2,101500.0,101000.0",
        "outlet,outlet_a,outward_positive,0.7,100900.0,100650.0",
        "outlet,outlet_b,outward_positive,0.5,100900.0,100650.0",
    ]
)
IMBALANCED = "\n".join(
    [
        HEADER,
        "inlet,inlet,outward_positive,-1.0,101500.0,101000.0",
        "outlet,outlet_a,outward_positive,0.9,100900.0,100650.0",
    ]
)
MONITOR = "\n".join(
    ["iteration,continuity", "1,1.0e-3", "2,5.0e-4", "3,2.5e-4"]
)


def _write(tmp_path, name: str, text: str) -> str:
    p = tmp_path / name
    p.write_text(text + "\n", encoding="utf-8")
    return str(p)


# 能力包真实骨架（全部阈值 null/TBD）
@pytest.fixture(scope="module")
def capability():
    return {
        "metric_defs": load_metric_definitions("buffer_chamber", "0.1.0"),
        "rules": load_rule_set("buffer_chamber", "0.1.0")[0],
        "domain": load_domain("buffer_chamber", "0.1.0"),
    }


FROZEN_METRIC_DEFS = {
    "metrics": [
        {"metric_id": "steady_mass_imbalance", "m_floor": 0.01, "unit": "dimensionless"},
        {"metric_id": "monitor_stability", "window": 3, "min_samples": 3, "unit": "1"},
        {"metric_id": "total_pressure_loss", "approved_sections": ["inlet"], "unit": "Pa"},
        {"metric_id": "outlet_distribution", "unit": "dimensionless"},
        {"metric_id": "boundary_mass_flow", "unit": "kg/s"},
    ]
}

CONFIRMED_DOMAIN = {
    "applicability": {
        "geometry_scope": "CONFIRMED",
        "condition_scope": "CONFIRMED",
        "physics_scope": "CONFIRMED",
        "usage_scope": "CONFIRMED",
    }
}


class TestParse:
    def test_parse_ok(self, tmp_path):
        samples, findings = parse_report_csv(_write(tmp_path, "r.csv", BALANCED))
        assert findings == []
        assert len(samples) == 3
        assert samples[0].mass_flow_kg_s == -1.2

    def test_missing_value_marked_not_zero(self, tmp_path):
        csv = HEADER + "\ninlet,inlet,outward_positive,,101500.0,101000.0"
        samples, findings = parse_report_csv(_write(tmp_path, "r.csv", csv))
        assert samples[0].mass_flow_kg_s is None  # null，不填 0
        assert any(f["kind"] == "MISSING_VALUE" for f in findings)

    def test_malformed_row_finding(self, tmp_path):
        csv = HEADER + "\ninlet,inlet,only_three_cols"
        samples, findings = parse_report_csv(_write(tmp_path, "r.csv", csv))
        assert samples == []
        assert any(f["kind"] == "MALFORMED_ROW" for f in findings)

    def test_missing_file_finding(self, tmp_path):
        samples, findings = parse_report_csv(tmp_path / "nope.csv")
        assert samples == [] and any(f["kind"] == "MISSING_FILE" for f in findings)


class TestExtract:
    def test_derived_metrics_deterministic(self, tmp_path, capability):
        out = extract_run_metrics(
            _write(tmp_path, "report.csv", BALANCED),
            _write(tmp_path, "monitor.csv", MONITOR),
            metric_definitions=capability["metric_defs"],
        )
        v = out.metric_values
        assert v["boundary_mass_flow@inlet"] == -1.2
        assert v["total_pressure@inlet"] == 101500.0
        # 出口分配 f_i = m_i / sum_out：0.7/1.2 与 0.5/1.2，和为 1
        assert v["outlet_distribution@outlet_a"] == pytest.approx(0.7 / 1.2)
        assert v["outlet_distribution@outlet_b"] == pytest.approx(0.5 / 1.2)
        # 总压损失 = 加权入口总压 − 加权出口总压 = 101500 − 100900
        assert v["total_pressure_loss"] == pytest.approx(600.0)
        # m_floor/监控窗口 null → 缺值 + finding（Owner 未冻结）
        assert v["steady_mass_imbalance"] is None
        assert v["monitor_stability"] is None
        assert "steady_mass_imbalance" in out.missing_metrics
        assert "monitor_stability" in out.missing_metrics
        unfrozen = [f for f in out.findings if f["kind"] == "UNFROZEN_THRESHOLD"]
        assert {f["metric_id"] for f in unfrozen} >= {
            "steady_mass_imbalance",
            "monitor_stability",
            "total_pressure_loss",
        }

    def test_frozen_thresholds_computable(self, tmp_path):
        out = extract_run_metrics(
            _write(tmp_path, "report.csv", BALANCED),
            _write(tmp_path, "monitor.csv", MONITOR),
            metric_definitions=FROZEN_METRIC_DEFS,
        )
        v = out.metric_values
        assert v["steady_mass_imbalance"] == pytest.approx(0.0)  # 平衡
        assert v["monitor_stability"] == pytest.approx(7.5e-4)
        assert out.missing_metrics == []

    def test_imbalance_nonzero(self, tmp_path):
        out = extract_run_metrics(
            _write(tmp_path, "report.csv", IMBALANCED), None,
            metric_definitions=FROZEN_METRIC_DEFS,
        )
        assert out.metric_values["steady_mass_imbalance"] == pytest.approx(0.1)

    def test_no_report_marks_missing(self, capability):
        out = extract_run_metrics(None, None, metric_definitions=capability["metric_defs"])
        assert "boundary_mass_flow" in out.missing_metrics
        assert out.metric_values["total_pressure_loss"] is None


class TestUnitSemantics:
    def test_gauge_without_reference_finding(self):
        spec = {
            "conditions": [
                {
                    "condition_id": "C1",
                    "fields": [
                        {
                            "role_id": "inlet",
                            "field": "static_pressure",
                            "quantity": {
                                "si_value": 5000.0,
                                "unit": "Pa",
                                "physical_meaning": "x",
                                "source_ref": "s",
                                "pressure_kind": "gauge",
                                "pressure_semantics": "static",
                                # 无 reference_absolute_pressure
                            },
                        }
                    ],
                }
            ]
        }
        findings = check_unit_semantics(spec)
        assert any(f["kind"] == "GAUGE_WITHOUT_REFERENCE" for f in findings)

    def test_pressure_field_non_pa_finding(self):
        spec = {
            "conditions": [
                {
                    "condition_id": "C1",
                    "fields": [
                        {
                            "role_id": "inlet",
                            "field": "total_pressure",
                            "quantity": {
                                "si_value": 1.0, "unit": "bar",
                                "physical_meaning": "x", "source_ref": "s",
                            },
                        }
                    ],
                }
            ]
        }
        assert any(f["kind"] == "UNIT_MISMATCH" for f in check_unit_semantics(spec))


class TestVerifier:
    def test_null_thresholds_all_unconfirmed(self, tmp_path, capability):
        """能力包骨架（阈值全 null）：每条规则 UNCONFIRMED，结论不能是 PASS。"""
        extracted = extract_run_metrics(
            _write(tmp_path, "report.csv", BALANCED),
            _write(tmp_path, "monitor.csv", MONITOR),
            metric_definitions=capability["metric_defs"],
        )
        result = verify_run(
            extracted=extracted, rules=capability["rules"], domain=capability["domain"]
        )
        unconfirmed = [f for f in result.findings if f["kind"] == "RULE_UNCONFIRMED"]
        rule_ids = {r["rule_id"] for r in capability["rules"]["rules"]}
        assert {f["rule_id"] for f in unconfirmed} == rule_ids
        assert all("Owner 未冻结" in f["message"] for f in unconfirmed)
        assert result.conclusion == "INSUFFICIENT"  # 缺值 + 未冻结 → 诚实不判 PASS
        assert result.applicability == "UNCONFIRMED"

    def test_frozen_rule_pass(self, tmp_path):
        extracted = extract_run_metrics(
            _write(tmp_path, "report.csv", BALANCED),
            _write(tmp_path, "monitor.csv", MONITOR),
            metric_definitions=FROZEN_METRIC_DEFS,
        )
        rules = {
            "status": "RELEASED",
            "rules": [
                {
                    "rule_id": "RULE-MASS-IMBALANCE",
                    "target_metric": "steady_mass_imbalance",
                    "threshold": 0.01,
                    "severity": "BLOCKER",
                }
            ],
        }
        result = verify_run(extracted=extracted, rules=rules, domain=CONFIRMED_DOMAIN)
        assert result.conclusion == "PASS"
        assert result.applicability == "IN_SCOPE"

    def test_frozen_rule_fail(self, tmp_path):
        extracted = extract_run_metrics(
            _write(tmp_path, "report.csv", IMBALANCED), None,
            metric_definitions=FROZEN_METRIC_DEFS,
        )
        rules = {
            "status": "RELEASED",
            "rules": [
                {
                    "rule_id": "RULE-MASS-IMBALANCE",
                    "target_metric": "steady_mass_imbalance",
                    "threshold": 0.05,
                    "severity": "BLOCKER",
                }
            ],
        }
        result = verify_run(extracted=extracted, rules=rules, domain=CONFIRMED_DOMAIN)
        assert result.conclusion == "FAIL"
        assert any(f["kind"] == "RULE_VIOLATED" for f in result.findings)

    def test_applicability_gate(self, capability):
        assert assess_applicability(capability["domain"]) == "UNCONFIRMED"
        assert assess_applicability(CONFIRMED_DOMAIN) == "IN_SCOPE"


class TestRecheckEndpoint:
    def test_recheck_produces_new_verification(self, client, tmp_path):
        from conftest import EXECUTOR_HEADERS
        from helpers_chain import full_mock_chain, ik

        chain = full_mock_chain(client, tmp_path, spec=None)  # 3 工况 × A/B
        run_id = chain.run_ids[0]
        before = client.get(
            f"/api/v1/verifications?run_id={run_id}", headers=EXECUTOR_HEADERS
        ).json()["items"]
        assert len(before) == 1

        r = client.post(
            "/api/v1/verifications/recheck",
            json={"run_id": run_id},
            headers={**EXECUTOR_HEADERS, **ik()},
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["run_id"] == run_id
        assert body["conclusion"] == "INSUFFICIENT"  # 阈值未冻结的诚实结论
        after = client.get(
            f"/api/v1/verifications?run_id={run_id}", headers=EXECUTOR_HEADERS
        ).json()["items"]
        assert len(after) == 2  # 新记录，旧记录不原位改写
        assert {v["verification_id"] for v in after} != {before[0]["verification_id"]}
