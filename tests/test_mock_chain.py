"""端到端 Mock 冒烟（FR-06..23 链路；定义书 §首版现场验收脚本 Mock 版）。

A/B×3 六 Run 全链路：createTask→prepare(Worker)→人工 authorize→submit×6→
Worker 执行→extract→verify→buildBundle→submitReview→issue DRAFT→confirm→
reply→close→decideReview。

诚实说明（与任务书对齐的偏差）：Mock 证据下 decideReview(ACCEPT) 按门必败
（负例一），因此正例链路的决定动作用 REQUEST_CHANGES 证明 decide 全链可达；
ACCEPT 成功路径在 tests/test_review_flow.py 以 REAL 标记的合成证据做门测试
（非 Mock 产物、非真实求解器输出，仅验证门逻辑与 FR-23 历史语义）。

负例三条：
1. MOCK 包 decideReview ACCEPT 必败（mock_evidence 逐条列因）；
2. 新修订后旧包/旧审查 STALE，新 decide 不受旧状态影响；
3. 缺一个 Run 的 bundle 标 incomplete 且 ACCEPT 必败。
"""
from __future__ import annotations

import pytest
from conftest import EXECUTOR_HEADERS, REVIEWER_HEADERS

from helpers_chain import (
    authorize_and_submit,
    build_bundle,
    create_task_with_revision,
    decide,
    full_mock_chain,
    ik,
    issue_confirm_reply_close,
    make_spec3,
    prepare_via_worker,
    run_worker,
    submit_review,
)

pytestmark = pytest.mark.mock


def test_full_mock_chain_end_to_end(client, tmp_path, capsys):
    """六 Run 全链路 Mock 跑通；issue 闭环；decide(REQUEST_CHANGES) 成功。"""
    chain = full_mock_chain(client, tmp_path)
    print(f"\n[chain] task={chain.task_id} runs={len(chain.run_ids)}")

    # 1) 六 Run 全部 SUCCEEDED；三维状态独立（数值受未冻结阈值影响为 INSUFFICIENT）
    r = client.get(f"/api/v1/tasks/{chain.task_id}/runs", headers=EXECUTOR_HEADERS)
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert len(items) == 6
    for run in items:
        assert run["execution_state"] == "SUCCEEDED", run
        assert run["numerical_state"] == "INSUFFICIENT"  # 阈值 Owner 未冻结 → 不可判 PASS
        assert run["applicability_state"] == "UNCONFIRMED"  # domain 范围未确认
        kinds = [e["kind"] for e in run["events"]]
        assert kinds[0] == "STARTING" and "RUNNING" in kinds and kinds[-1] == "COMPLETED"
        hb = [e for e in run["events"] if e["kind"] == "HEARTBEAT"]
        assert hb and all("stage" in e["payload"] for e in hb)
        done = [e for e in run["events"] if e["kind"] == "COMPLETED"][0]
        assert done["payload"]["exit_code"] == 0
        assert done["payload"]["evidence_mode"] == "MOCK"
        assert done["payload"]["artifact_ids"]
    print(f"[chain] 6 runs SUCCEEDED; events/run={[len(r0['events']) for r0 in items]}")

    # 2) Verification：每 Run 一条，阈值 null → RULE_UNCONFIRMED finding（不编数）
    r = client.get(f"/api/v1/verifications?task_id={chain.task_id}", headers=EXECUTOR_HEADERS)
    vers = r.json()["items"]
    assert len(vers) == 6
    assert all(v["conclusion"] == "INSUFFICIENT" for v in vers)
    assert any(f["kind"] == "RULE_UNCONFIRMED" for v in vers for f in v["findings"])
    print(f"[chain] verifications=6 conclusion=INSUFFICIENT (rules TBD)")

    # 3) Bundle：complete；manifest 含 task_spec/preparation/raw/metrics/verification/report
    bundle = chain.bundle
    assert bundle["validity"] == "CURRENT"
    # 全量冻结清单（含 role/length）走投影端点；契约 Bundle 响应为 3 字段引用形
    r = client.get(f"/api/v1/bundles/{bundle['bundle_id']}/manifest", headers=EXECUTOR_HEADERS)
    assert r.status_code == 200, r.text
    full_manifest = r.json()["manifest"]
    roles = {e["role"] for e in full_manifest}
    assert {"task_spec", "source_refs", "preparation", "rules", "method_metrics"} <= roles
    assert "raw" in roles and "metrics" in roles and "verification" in roles and "report" in roles
    assert all(e["length"] > 0 and e["sha256"] for e in full_manifest)
    r = client.get(f"/api/v1/tasks/{chain.task_id}/bundles/latest", headers=EXECUTOR_HEADERS)
    latest = r.json()
    assert latest["complete"] is True
    assert latest["evidence_mode"] == "MOCK"
    print(f"[chain] bundle={bundle['bundle_id']} complete=True mode=MOCK manifest={len(full_manifest)}")

    # 4) Claims / metrics 投影
    r = client.get(f"/api/v1/bundles/{bundle['bundle_id']}/claims", headers=EXECUTOR_HEADERS)
    claims = r.json()["items"]
    assert claims and all(c["metric_id"] and c["artifact_id"] for c in claims)
    assert any(c["state"] == "CONFIRMED" for c in claims)
    assert any(c["state"] == "DRAFT" for c in claims)  # null 阈值指标不冒充已确认
    r = client.get(f"/api/v1/bundles/{bundle['bundle_id']}/metrics", headers=EXECUTOR_HEADERS)
    metrics = r.json()["items"]
    assert any(m["metric_id"] == "total_pressure_loss" and m["value"] == 600.0 for m in metrics)
    assert any(m["metric_id"] == "steady_mass_imbalance" and m["value"] is None and m["missing"] for m in metrics)
    print(f"[chain] claims={len(claims)} metrics={len(metrics)}")

    # 5) 任务/准备投影
    r = client.get("/api/v1/tasks?filter=pending_review", headers=EXECUTOR_HEADERS)
    assert any(t["task_id"] == chain.task_id for t in r.json()["items"])
    r = client.get(f"/api/v1/tasks/{chain.task_id}/preparations/latest", headers=EXECUTOR_HEADERS)
    prep = r.json()
    assert prep["ready"] is True and prep["differences"] == [] and prep["evidence_mode"] == "MOCK"

    # 6) issue DRAFT→confirm→reply→close 闭环；decide(REQUEST_CHANGES) 成功
    evidence_art = next(e["artifact_id"] for e in full_manifest if e["role"] == "verification")
    issue = issue_confirm_reply_close(client, chain.review, bundle, evidence_art)
    print(f"[chain] issue={issue['issue_id']} CLOSED by {issue['closed_by']}")

    r = decide(client, chain.review, "REQUEST_CHANGES", limitations="MOCK 演示链退回")
    assert r.status_code == 201, r.text
    assert r.json()["outcome"] == "REQUEST_CHANGES"
    print(f"[chain] decide REQUEST_CHANGES 201 OK")

    # 7) 审查历史可读
    r = client.get(f"/api/v1/tasks/{chain.task_id}/review-history", headers=REVIEWER_HEADERS)
    history = r.json()["items"]
    assert history and history[0]["decision"]["outcome"] == "REQUEST_CHANGES"


def test_negative_mock_bundle_accept_must_fail(client, tmp_path):
    """负例 1：MOCK 包 decideReview ACCEPT 必败（FR-31；仍允许 REQUEST_CHANGES/REJECT）。"""
    chain = full_mock_chain(client, tmp_path)
    r = decide(client, chain.review, "ACCEPT")
    assert r.status_code == 503, r.text
    body = r.json()
    assert body["code"] == "BLOCKED" and body["retryable"] is False
    details = body["details"]
    assert "mock_evidence" in details  # MOCK artifact 逐条列因
    assert "numerical_insufficient" in details
    assert "applicability_unconfirmed" in details
    print(f"\n[neg1] ACCEPT→503 details_keys={sorted(details)}")


def test_negative_new_revision_stales_bundle_and_review(client, tmp_path):
    """负例 2：新修订生效 → 旧 bundle/旧 review STALE；旧 decide 被拒；新 decide 不受影响（FR-23）。"""
    chain = full_mock_chain(client, tmp_path)

    # 新修订 R2（改一个边界数值）
    spec2 = make_spec3()
    spec2["conditions"][0]["fields"][0]["quantity"]["si_value"] = 103000.0
    r = client.post(
        f"/api/v1/tasks/{chain.task_id}/revisions",
        json={"expected_revision": 1, "spec": spec2},
        headers={**EXECUTOR_HEADERS, **ik()},
    )
    assert r.status_code == 201, r.text
    assert r.json()["revision"] == 2

    # 旧包/旧审查 STALE；历史记录保留可读
    r = client.get(f"/api/v1/bundles/{chain.bundle['bundle_id']}", headers=EXECUTOR_HEADERS)
    assert r.json()["validity"] == "STALE"
    r = client.get(f"/api/v1/reviews/{chain.review['review_id']}", headers=REVIEWER_HEADERS)
    assert r.json()["validity"] == "STALE"
    print(f"\n[neg2] old bundle & review → STALE")

    # 旧 review 上 decide：修订有效性检查失败（409）
    r = decide(client, chain.review, "REQUEST_CHANGES", revision=1)
    assert r.status_code == 409, r.text
    assert r.json()["code"] == "CONFLICT_REVISION"

    # 新修订走新链（Mock）→ 新 decide 不受旧状态影响
    prep2 = prepare_via_worker(client, tmp_path, chain.task_id, revision=2)
    run_ids2 = authorize_and_submit(client, chain.task_id, prep2, revision=2)
    assert len(run_ids2) == 6
    run_worker(client, tmp_path)
    bundle2 = build_bundle(client, chain.task_id, revision=2)
    assert bundle2["validity"] == "CURRENT"
    review2 = submit_review(client, chain.task_id, bundle2, revision=2)
    r = decide(client, review2, "REQUEST_CHANGES", revision=2)
    assert r.status_code == 201, r.text

    # 历史两态同时可读：R1（STALE 旧审查）与 R2（新决定）
    r = client.get(f"/api/v1/tasks/{chain.task_id}/review-history", headers=REVIEWER_HEADERS)
    history = r.json()["items"]
    by_rev = {h["revision"]: h for h in history}
    assert by_rev[1]["validity"] == "STALE" and by_rev[1]["decision"] is None
    assert by_rev[2]["decision"]["outcome"] == "REQUEST_CHANGES"
    print(f"[neg2] R1 STALE preserved; R2 decide 201 OK; history={len(history)}")


def test_negative_missing_run_bundle_incomplete_accept_fails(client, tmp_path):
    """负例 3：缺一个 Run 未执行 → bundle 标 incomplete 且 ACCEPT 必败（不能减清单通过）。"""
    chain = full_mock_chain(client, tmp_path, execute_max_jobs=5)  # 只执行 5/6 个 EXECUTE

    r = client.get(f"/api/v1/tasks/{chain.task_id}/bundles/latest", headers=EXECUTOR_HEADERS)
    latest = r.json()
    assert latest["complete"] is False
    kinds = {m["kind"] for m in latest["incomplete_items"]}
    assert "RUN_NOT_SUCCEEDED" in kinds
    print(f"\n[neg3] bundle incomplete: {sorted(kinds)}")

    r = decide(client, chain.review, "ACCEPT")
    assert r.status_code == 503, r.text
    details = r.json()["details"]
    assert "bundle_incomplete" in details
    assert "execution_not_succeeded" in details
    print(f"[neg3] ACCEPT→503 bundle_incomplete+execution_not_succeeded")
