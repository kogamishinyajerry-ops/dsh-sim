"""测试共享链路辅助（mock 层）：A/B×N 工况全链路驱动 + Worker 直调。

Worker 通道：直接调 dsh_sim.worker.loop.run_until_idle（同进程，见 loop.py 选择说明）。
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace

from conftest import EXECUTOR_HEADERS, FAKE_SHA, REVIEWER_HEADERS, make_spec


def ik() -> dict[str, str]:
    return {"Idempotency-Key": uuid.uuid4().hex * 2}


def make_spec3(**overrides) -> dict:
    """A/B × 3 工况的合法 TaskSpec（端到端六 Run 用）。"""
    spec = make_spec()
    base_field = spec["conditions"][0]["fields"][0]
    spec["conditions"] = [
        {
            "condition_id": cid,
            "fields": [
                {
                    **base_field,
                    "quantity": {
                        **base_field["quantity"],
                        "si_value": 101325.0 + i * 500.0,
                        "source_ref": f"spec-sheet-00{i + 1}",
                    },
                }
            ],
        }
        for i, cid in enumerate(["C1", "C2", "C3"])
    ]
    spec.update(overrides)
    return spec


def run_worker(client, tmp_path, *, behavior: str = "staged", max_jobs=None, node_id="node-test"):
    """同步跑 Worker 直到队列空，返回处理的 job_id 列表。"""
    from dsh_sim.adapters.mock_adapter import MockStarAdapter
    from dsh_sim.worker.loop import WorkerConfig, run_until_idle

    factory = client.app.state.session_factory
    s = factory()
    config = WorkerConfig(
        node_id=node_id,
        work_root=tmp_path / "worker",
        artifact_root=client.app.state.artifact_root,
        max_jobs=max_jobs,
    )
    try:
        return run_until_idle(s, MockStarAdapter(behavior=behavior), config)
    finally:
        s.close()


def create_task_with_revision(client, spec=None) -> str:
    spec = spec or make_spec3()
    r = client.post(
        "/api/v1/tasks",
        json={"draft": {"purpose": "design_screening"}},
        headers={**EXECUTOR_HEADERS, **ik()},
    )
    assert r.status_code == 201, r.text
    task_id = r.json()["task_id"]
    r = client.post(
        f"/api/v1/tasks/{task_id}/revisions",
        json={"expected_revision": 0, "spec": spec},
        headers={**EXECUTOR_HEADERS, **ik()},
    )
    assert r.status_code == 201, r.text
    return task_id


def prepare_via_worker(client, tmp_path, task_id: str, revision: int = 1) -> dict:
    """prepareTask → Worker 执行 PREPARE → 返回准备投影。"""
    r = client.post(
        f"/api/v1/tasks/{task_id}/prepare",
        json={"revision": revision},
        headers={**EXECUTOR_HEADERS, **ik()},
    )
    assert r.status_code == 202, r.text
    preparation_id = r.json()["preparation_id"]
    run_worker(client, tmp_path)
    r = client.get(f"/api/v1/preparations/{preparation_id}", headers=EXECUTOR_HEADERS)
    assert r.status_code == 200, r.text
    return r.json()


def authorize_and_submit(client, task_id: str, preparation: dict, revision: int = 1) -> list[str]:
    """人工确认 → authorizeRuns → submitRuns，返回 run_ids。"""
    digest = preparation["prepared_digest"]
    r = client.post(
        "/api/v1/confirmations",
        json={"action": "authorizeRuns", "target_id": task_id, "target_digest": digest},
        headers={**EXECUTOR_HEADERS, **ik()},
    )
    assert r.status_code == 201, r.text
    confirmation_id = r.json()["confirmation_id"]
    r = client.post(
        f"/api/v1/tasks/{task_id}/authorizations",
        json={
            "revision": revision,
            "preparation_id": preparation["preparation_id"],
            "prepared_digest": digest,
            "execution_budget": make_spec()["execution_budget"],
            "confirmation_id": confirmation_id,
        },
        headers={**EXECUTOR_HEADERS, **ik()},
    )
    assert r.status_code == 201, r.text
    auth_id = r.json()["authorization_id"]
    r = client.post(
        f"/api/v1/tasks/{task_id}/submissions",
        json={"authorization_id": auth_id, "prepared_digest": digest},
        headers={**EXECUTOR_HEADERS, **ik()},
    )
    assert r.status_code == 202, r.text
    return r.json()["run_ids"]


def build_bundle(client, task_id: str, revision: int = 1) -> dict:
    r = client.post(
        f"/api/v1/tasks/{task_id}/bundles",
        json={"revision": revision},
        headers={**EXECUTOR_HEADERS, **ik()},
    )
    assert r.status_code == 201, r.text
    return r.json()


def submit_review(client, task_id: str, bundle: dict, revision: int = 1) -> dict:
    r = client.post(
        f"/api/v1/tasks/{task_id}/reviews",
        json={
            "revision": revision,
            "bundle_id": bundle["bundle_id"],
            "bundle_digest": bundle["bundle_digest"],
            "reviewer_id": REVIEWER_HEADERS["X-Dev-Subject"],
        },
        headers={**EXECUTOR_HEADERS, **ik()},
    )
    assert r.status_code == 201, r.text
    return r.json()


def decide(client, review: dict, outcome: str, *, revision: int = 1, limitations=None):
    """人工确认 → decideReview。返回响应对象（不断言状态码）。"""
    r = client.post(
        "/api/v1/confirmations",
        json={
            "action": "decideReview",
            "target_id": review["review_id"],
            "target_digest": review["bundle_digest"],
        },
        headers={**REVIEWER_HEADERS, **ik()},
    )
    assert r.status_code == 201, r.text
    confirmation_id = r.json()["confirmation_id"]
    return client.post(
        f"/api/v1/reviews/{review['review_id']}/decisions",
        json={
            "outcome": outcome,
            "bundle_digest": review["bundle_digest"],
            "revision": revision,
            "limitations": limitations,
            "confirmation_id": confirmation_id,
        },
        headers={**REVIEWER_HEADERS, **ik()},
    )


def full_mock_chain(client, tmp_path, *, spec=None, execute_max_jobs=None) -> SimpleNamespace:
    """全链路 Mock：createTask→prepare(Worker)→authorize→submit→Worker 执行→bundle→review。"""
    task_id = create_task_with_revision(client, spec)
    prep = prepare_via_worker(client, tmp_path, task_id)
    assert prep["blockers"] == [], prep
    run_ids = authorize_and_submit(client, task_id, prep)
    run_worker(client, tmp_path, max_jobs=execute_max_jobs)
    bundle = build_bundle(client, task_id)
    review = submit_review(client, task_id, bundle)
    return SimpleNamespace(
        task_id=task_id, preparation=prep, run_ids=run_ids, bundle=bundle, review=review
    )


def issue_confirm_reply_close(client, review: dict, bundle: dict, evidence_artifact_id: str) -> dict:
    """issue DRAFT → confirm → reply(带证据) → close，返回最终 issue。"""
    r = client.post(
        f"/api/v1/reviews/{review['review_id']}/issues",
        json={
            "responsible": "eng_zhang",
            "severity": "MAJOR",
            "description": "压损比较口径是否与批准截面一致？",
            "close_criteria": "提供同截面同定义的独立复算证据",
        },
        headers={**REVIEWER_HEADERS, **ik()},
    )
    assert r.status_code == 201, r.text
    issue = r.json()
    assert issue["status"] == "DRAFT"

    digest = bundle["bundle_digest"]
    r = client.post(
        "/api/v1/confirmations",
        json={"action": "confirmIssue", "target_id": issue["issue_id"], "target_digest": digest},
        headers={**REVIEWER_HEADERS, **ik()},
    )
    conf_id = r.json()["confirmation_id"]
    r = client.post(
        f"/api/v1/issues/{issue['issue_id']}/confirm",
        json={"issue_version": issue["version"], "confirmation_id": conf_id},
        headers={**REVIEWER_HEADERS, **ik()},
    )
    assert r.status_code == 200, r.text
    issue = r.json()
    assert issue["status"] == "OPEN"

    r = client.post(
        f"/api/v1/issues/{issue['issue_id']}/replies",
        json={"body": "已补充同口径复算证据", "evidence_artifact_ids": [evidence_artifact_id]},
        headers={**EXECUTOR_HEADERS, **ik()},
    )
    assert r.status_code == 201, r.text
    issue = r.json()

    r = client.post(
        "/api/v1/confirmations",
        json={"action": "closeIssue", "target_id": issue["issue_id"], "target_digest": digest},
        headers={**REVIEWER_HEADERS, **ik()},
    )
    conf_id = r.json()["confirmation_id"]
    r = client.post(
        f"/api/v1/issues/{issue['issue_id']}/close",
        json={
            "issue_version": issue["version"],
            "close_evidence_artifact_ids": [evidence_artifact_id],
            "confirmation_id": conf_id,
        },
        headers={**REVIEWER_HEADERS, **ik()},
    )
    assert r.status_code == 200, r.text
    issue = r.json()
    assert issue["status"] == "CLOSED"
    assert issue["closed_by"] == REVIEWER_HEADERS["X-Dev-Subject"]
    return issue


__all__ = [
    "authorize_and_submit",
    "build_bundle",
    "create_task_with_revision",
    "decide",
    "full_mock_chain",
    "ik",
    "issue_confirm_reply_close",
    "make_spec3",
    "prepare_via_worker",
    "run_worker",
    "submit_review",
]
