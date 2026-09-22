"""dsh-sim 证据层（Agent E / WP-13）：Bundle 冻结 + Jinja 确定性报告。"""
from dsh_sim.evidence.bundle import (
    build_bundle,
    bundle_evidence_mode,
    compute_completeness,
)
from dsh_sim.evidence.report import render_report

__all__ = [
    "build_bundle",
    "bundle_evidence_mode",
    "compute_completeness",
    "render_report",
]
