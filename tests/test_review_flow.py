"""审查闭环门测试（FR-19..23；WP-14/16）。

⚠️ 数据性质声明：本文件直接构造 **REAL 标记的合成证据**（非 MockStarAdapter 产物、
非真实求解器输出），仅用于验证 ACCEPT 门逻辑、issue 闭环与 FR-23 历史语义。
MOCK 链路的端到端证据见 tests/test_mock_chain.py；真实求解器证据 NOT_RUN（P05 限定）。
"""
from __future__ import annotations

import hashlib
import uuid
from pathlib import Path

import pytest
from conftest import AGENT_HEADERS, EXECUTOR_HEADERS, REVIEWER_HEADERS, make_spec

from dsh_sim.db.models import ArtifactRow, AttemptRow, RunRow, VerificationRow

from helpers_chain import (
    authorize_and_submit,
    build_bundle,
    create_task_with_revision,
    decide,
    ik,
    prepare_via_worker,
    submit_review,
)

pytestmark = pytest.mark.mock

REAL_CSV = "\n".join(
    [
        "section,boundary_role,sign_convention,mass_flow_kg_s,total_pressure_pa,static_pressure_pa",
        "inlet,inlet,outward_positive,-1.2,101500.0,101000.0",
        "outlet,outlet_a,outward_positive,0.7,100900.0,100650.0",
        "outlet,outlet_b,outward_positive,0.5,100900.0,100650.0",
    ]
)


def _real_tagged_runs(client, task_id: str, run_ids: list[str], revision: int = 1) -> None:
    """把 submit 产生的 Run 直接落库为"REAL 标记合成证据完成态"（门测试夹具）。"""
    s = client.app.state.session_factory()
    artifact_root = client.app.state.artifact_root
    try:
        for run_id in run_ids:
            run = s.get(RunRow, run_id)
            run.execution_state = "SUCCEEDED"
            run.numerical_state = "PASS"
            run.applicability_state = "IN_SCOPE"
            attempt = s.get(AttemptRow, run.current_attempt_id)
            attempt.state = "SUCCEEDED"

            content = (REAL_CSV + "\n").encode("utf-8")
            sha = hashlib.sha256(content).hexdigest()
            art_id = f"art_{uuid.uuid4().hex[:24]}"
            dest_dir = Path(artifact_root) / "proj_a"
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / art_id
            dest.write_bytes(content)
            s.add(
                ArtifactRow(
                    artifact_id=art_id,
                    project_id="proj_a",
                    logical_path=f"runs/{run_id}/attempt-1/raw/report.csv",
                    length=len(content),
                    sha256=sha,
                    state="COMMITTED",
                    run_id=run_id,
                    attempt_id=attempt.attempt_id,
                    evidence_mode="REAL",  # 门测试夹具：非 Mock 产物、非真实求解器输出
                    storage_path=str(dest),
                )
            )
            # metrics artifact（Claim 生成需要）
            import json as _json

            mcontent = _json.dumps(
                {
                    "evidence_mode": "REAL",
                    "metric_values": {
                        "boundary_mass_flow@inlet": -1.2,
                        "boundary_mass_flow@outlet_a": 0.7,
                        "boundary_mass_flow@outlet_b": 0.5,
                        "total_pressure@inlet": 101500.0,
                        "total_pressure@outlet_a": 100900.0,
                        "total_pressure@outlet_b": 100900.0,
                    },
                },
                ensure_ascii=False,
            ).encode("utf-8")
            mid = f"art_{uuid.uuid4().hex[:24]}"
            mdest = dest_dir / mid
            mdest.write_bytes(mcontent)
            s.add(
                ArtifactRow(
                    artifact_id=mid,
                    project_id="proj_a",
                    logical_path=f"runs/{run_id}/attempt-1/metrics.json",
                    length=len(mcontent),
                    sha256=hashlib.sha256(mcontent).hexdigest(),
                    state="COMMITTED",
                    run_id=run_id,
                    attempt_id=attempt.attempt_id,
                    evidence_mode="REAL",
                    storage_path=str(mdest),
                )
            )
            s.add(
                VerificationRow(
                    verification_id=f"ver_{uuid.uuid4().hex[:24]}",
                    run_id=run_id,
                    attempt_id=attempt.attempt_id,
                    rule_set_sha256="0" * 64,
                    check_inputs={"fixture": "real-tagged gate test"},
                    conclusion="PASS",
                    findings=[],
                    source_artifact_ids=[art_id],
                )
            )
        s.commit()
    finally:
        s.close()


def _setup_real_review(client, tmp_path, *, revision_spec=None) -> dict:
    """任务 → 准备（Worker mock 准备产物，run_id=None 不影响门）→ 授权提交 →
    REAL 标记合成证据 → buildBundle → submitReview。"""
    task_id = create_task_with_revision(client, spec=revision_spec or make_spec())
    prep = prepare_via_worker(client, tmp_path, task_id)
    run_ids = authorize_and_submit(client, task_id, prep)
    _real_tagged_runs(client, task_id, run_ids)
    bundle = build_bundle(client, task_id)
    review = submit_review(client, task_id, bundle)
    return {
        "task_id": task_id,
        "run_ids": run_ids,
        "bundle": bundle,
        "review": review,
        "prep": prep,
    }


class TestAcceptGate:
    def test_accept_success(self, client, tmp_path):
        """全部门通过（REAL 标记合成证据）：decideReview ACCEPT 成功。"""
        ctx = _setup_real_review(client, tmp_path)
        r = decide(client, ctx["review"], "ACCEPT", limitations="仅限内部方案筛选")
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["outcome"] == "ACCEPT"
        assert body["decided_by"] == REVIEWER_HEADERS["X-Dev-Subject"]
        assert body["limitations"] == "仅限内部方案筛选"
        print(f"\n[gate] ACCEPT 201 decided_by={body['decided_by']}")

    def test_open_issue_blocks_accept_until_closed(self, client, tmp_path):
        """DRAFT/OPEN 问题逐条阻塞；回复+证据+审查人关闭后 ACCEPT 才放行（FR-21）。"""
        ctx = _setup_real_review(client, tmp_path)
        review, bundle = ctx["review"], ctx["bundle"]

        # DRAFT 问题阻塞
        r = client.post(
            f"/api/v1/reviews/{review['review_id']}/issues",
            json={
                "responsible": "eng_zhang",
                "severity": "BLOCKER",
                "description": "出口分配口径质疑",
                "close_criteria": "提供同口径复算证据",
            },
            headers={**REVIEWER_HEADERS, **ik()},
        )
        issue = r.json()
        assert issue["status"] == "DRAFT"
        r = decide(client, review, "ACCEPT")
        assert r.status_code == 503 and "draft_issues_unconfirmed" in r.json()["details"]

        # confirm → OPEN 仍阻塞
        r = client.post(
            "/api/v1/confirmations",
            json={
                "action": "confirmIssue",
                "target_id": issue["issue_id"],
                "target_digest": bundle["bundle_digest"],
            },
            headers={**REVIEWER_HEADERS, **ik()},
        )
        conf = r.json()["confirmation_id"]
        r = client.post(
            f"/api/v1/issues/{issue['issue_id']}/confirm",
            json={"issue_version": issue["version"], "confirmation_id": conf},
            headers={**REVIEWER_HEADERS, **ik()},
        )
        assert r.json()["status"] == "OPEN"
        r = decide(client, review, "ACCEPT")
        assert r.status_code == 503 and "open_issue_ids" in r.json()["details"]

        # 答复文本本身不能关闭；回复+证据 → 审查人关闭 → ACCEPT 放行
        evidence_art = ctx["bundle"]["manifest"][0]["artifact_id"]
        r = client.post(
            f"/api/v1/issues/{issue['issue_id']}/replies",
            json={"body": "已补充证据", "evidence_artifact_ids": [evidence_art]},
            headers={**EXECUTOR_HEADERS, **ik()},
        )
        issue = r.json()
        r = client.post(
            "/api/v1/confirmations",
            json={
                "action": "closeIssue",
                "target_id": issue["issue_id"],
                "target_digest": bundle["bundle_digest"],
            },
            headers={**REVIEWER_HEADERS, **ik()},
        )
        conf = r.json()["confirmation_id"]
        r = client.post(
            f"/api/v1/issues/{issue['issue_id']}/close",
            json={
                "issue_version": issue["version"],
                "close_evidence_artifact_ids": [evidence_art],
                "confirmation_id": conf,
            },
            headers={**REVIEWER_HEADERS, **ik()},
        )
        assert r.status_code == 200 and r.json()["status"] == "CLOSED"
        r = decide(client, review, "ACCEPT")
        assert r.status_code == 201, r.text
        print(f"\n[gate] issue 闭环后 ACCEPT 201")

    def test_close_requires_evidence(self, client, tmp_path):
        """关闭必须附证据（422）；Agent 不能 confirm/close（403）。"""
        ctx = _setup_real_review(client, tmp_path)
        review = ctx["review"]
        r = client.post(
            f"/api/v1/reviews/{review['review_id']}/issues",
            json={
                "responsible": "eng_zhang",
                "severity": "MAJOR",
                "description": "d",
                "close_criteria": "c",
            },
            headers={**REVIEWER_HEADERS, **ik()},
        )
        issue = r.json()
        # Agent 身份 confirm → 403（身份门，Agent 只能 DRAFT/回复）
        r = client.post(
            f"/api/v1/issues/{issue['issue_id']}/confirm",
            json={"issue_version": 1, "confirmation_id": "conf_x"},
            headers={**AGENT_HEADERS, **ik()},
        )
        assert r.status_code == 403
        r = client.post(
            f"/api/v1/issues/{issue['issue_id']}/close",
            json={
                "issue_version": 1,
                "close_evidence_artifact_ids": ["art_x"],
                "confirmation_id": "conf_x",
            },
            headers={**AGENT_HEADERS, **ik()},
        )
        assert r.status_code == 403
        # 审查人先 confirm（DRAFT→OPEN），再关闭但无证据 → 422（证据门先于状态门之后校验）
        r = client.post(
            "/api/v1/confirmations",
            json={
                "action": "confirmIssue",
                "target_id": issue["issue_id"],
                "target_digest": ctx["bundle"]["bundle_digest"],
            },
            headers={**REVIEWER_HEADERS, **ik()},
        )
        conf_id = r.json()["confirmation_id"]
        r = client.post(
            f"/api/v1/issues/{issue['issue_id']}/confirm",
            json={"issue_version": 1, "confirmation_id": conf_id},
            headers={**REVIEWER_HEADERS, **ik()},
        )
        assert r.status_code == 200, r.text
        r = client.post(
            f"/api/v1/issues/{issue['issue_id']}/close",
            json={
                "issue_version": r.json()["version"],
                "close_evidence_artifact_ids": [],
                "confirmation_id": "conf_x",
            },
            headers={**REVIEWER_HEADERS, **ik()},
        )
        assert r.status_code == 422


class TestHistoryFR23:
    def test_accepted_history_preserved_new_revision_not_inherited(self, client, tmp_path):
        """R1 ACCEPT 后新建 R2：旧 bundle/review STALE、旧决定保留可读、
        当前 R2 未继承接受（"当时接受 R1"与"当前 R2 尚未接受"两态可读）。"""
        ctx = _setup_real_review(client, tmp_path)
        r = decide(client, ctx["review"], "ACCEPT")
        assert r.status_code == 201, r.text
        decision_id = r.json()["decision_id"]

        # 新修订 R2
        spec2 = make_spec()
        spec2["conditions"][0]["fields"][0]["quantity"]["si_value"] = 102500.0
        r = client.post(
            f"/api/v1/tasks/{ctx['task_id']}/revisions",
            json={"expected_revision": 1, "spec": spec2},
            headers={**EXECUTOR_HEADERS, **ik()},
        )
        assert r.status_code == 201, r.text

        # 旧包/旧审查 STALE；任务回到 DRAFT、review_state NOT_SUBMITTED（不继承接受）
        r = client.get(f"/api/v1/bundles/{ctx['bundle']['bundle_id']}", headers=EXECUTOR_HEADERS)
        assert r.json()["validity"] == "STALE"
        r = client.get(f"/api/v1/reviews/{ctx['review']['review_id']}", headers=REVIEWER_HEADERS)
        assert r.json()["validity"] == "STALE"
        r = client.get(f"/api/v1/tasks/{ctx['task_id']}", headers=EXECUTOR_HEADERS)
        assert r.json()["task_state"] == "DRAFT"
        assert r.json()["review_state"] == "NOT_SUBMITTED"

        # 历史两态：R1 当时接受（决定保留）+ R2 尚未接受
        r = client.get(
            f"/api/v1/tasks/{ctx['task_id']}/review-history", headers=REVIEWER_HEADERS
        )
        history = r.json()["items"]
        r1 = next(h for h in history if h["revision"] == 1)
        assert r1["decision"]["decision_id"] == decision_id
        assert r1["decision"]["outcome"] == "ACCEPT"
        assert r1["validity"] == "STALE"  # 对当前输入失效，而非删除
        assert not any(h["revision"] == 2 for h in history)  # R2 尚无审查
        print(f"\n[fr23] R1 ACCEPT preserved (STALE); R2 NOT_SUBMITTED")

        # 旧 review 上的任何 decide 被拒（修订有效性）
        r = decide(client, ctx["review"], "ACCEPT", revision=1)
        assert r.status_code == 409

        # 旧决定记录不可变：直接改库被硬守卫拒绝
        from dsh_sim.db.models import DecisionRow
        from dsh_sim.domain.errors import ApiError

        s = client.app.state.session_factory()
        try:
            d = s.get(DecisionRow, decision_id)
            d.limitations = "tamper"
            with pytest.raises(ApiError):
                s.flush()
        finally:
            s.rollback()
            s.close()
