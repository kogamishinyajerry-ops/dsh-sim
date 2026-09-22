"""有据报告（FR-18；定义书 §证据包、结论与可追溯性）。

- Jinja 确定性渲染：模板只由维护者提供（templates/report.html.j2），
  渲染数据全部来自已冻结的数据库对象，不接受不可信用户模板。
- 每条定量 Claim 绑定 metric_id + artifact_id，可展开至原始 artifact。
- 页眉常驻 evidence_mode；MOCK 时整页斜纹水印 + 重复 MOCK 字样（肉眼可区分）。
- 缺项和限制单独一节；多维状态逐 Run 列出，不用一个绿勾代替全部判断。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy.orm import Session

from dsh_sim.db.models import ClaimRow, RunRow, TaskRevisionRow, TaskRow

_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"

_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATE_DIR)),
    autoescape=select_autoescape(["html", "j2"]),
    trim_blocks=True,
    lstrip_blocks=True,
)


def render_report(
    session: Session,
    *,
    bundle_id: str,
    manifest_digest: str,
    task: TaskRow,
    rev: TaskRevisionRow,
    runs: list[RunRow],
    claims: list[ClaimRow],
    evidence_mode: str,
    completeness: dict[str, Any],
    manifest: list[dict[str, Any]],
) -> str:
    spec = rev.spec
    method = spec.get("method") or {}
    template = _env.get_template("report.html.j2")
    return template.render(
        bundle_id=bundle_id,
        manifest_digest=manifest_digest,
        task_id=task.task_id,
        revision=rev.revision,
        spec_sha256=rev.spec_sha256,
        owner_id=task.owner_id,
        purpose=task.purpose,
        review_scope=method.get("review_scope", ""),
        capability_package_id=method.get("capability_package_id", ""),
        evidence_mode=evidence_mode,
        is_mock=evidence_mode == "MOCK",
        # MOCK 水印重复网格（确定性 4×6，不依赖随机/时间）
        watermark_cells=range(24) if evidence_mode == "MOCK" else (),
        runs=[
            {
                "run_id": r.run_id,
                "variant_id": r.variant_id,
                "condition_id": r.condition_id,
                "execution_state": r.execution_state,
                "numerical_state": r.numerical_state,
                "applicability_state": r.applicability_state,
            }
            for r in runs
        ],
        claims=[
            {
                "claim_id": c.claim_id,
                "metric_id": c.metric_id,
                "artifact_id": c.artifact_id,
                "text": c.text,
                "author_type": c.author_type,
                "state": c.state,
            }
            for c in claims
        ],
        incomplete_items=completeness["missing"],
        complete=completeness["complete"],
        limitations="仅限内部方案筛选用途；不构成适航符合性结论。",
        manifest=manifest,
    )


__all__ = ["render_report"]
