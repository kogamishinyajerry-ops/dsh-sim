"""API 契约测试（mock 层）：哈希集成、幂等、权限、职责分离、队列端点、附件流。

与 contracts/openapi.v0.1.yaml 的 operationId 一一对应；错误模型与 HTTP 映射按
CONVENTIONS §3.3 校验。
"""
from __future__ import annotations

import hashlib
import uuid

import pytest

from dsh_sim.canonical import spec_sha256
from dsh_sim.db.models import BundleRow
from dsh_sim.domain.schemas import TaskSpec

from conftest import (
    AGENT_HEADERS,
    EXECUTOR_HEADERS,
    NODE_HEADERS,
    OTHER_PROJECT_HEADERS,
    REVIEWER_HEADERS,
    make_spec,
)

pytestmark = pytest.mark.mock


def ik() -> dict[str, str]:
    return {"Idempotency-Key": uuid.uuid4().hex * 2}


def _create_task_with_revision(client, spec=None) -> tuple[str, str]:
    """建任务 + 修订 1，返回 (task_id, spec_sha256)。"""
    spec = spec or make_spec()
    r = client.post(
        "/api/v1/tasks", json={"draft": {"purpose": "design_screening"}}, headers={**EXECUTOR_HEADERS, **ik()}
    )
    assert r.status_code == 201, r.text
    task_id = r.json()["task_id"]
    r = client.post(
        f"/api/v1/tasks/{task_id}/revisions",
        json={"expected_revision": 0, "spec": spec},
        headers={**EXECUTOR_HEADERS, **ik()},
    )
    assert r.status_code == 201, r.text
    return task_id, r.json()["spec_sha256"]


def _prepare_and_authorize(client, task_id: str) -> tuple[str, str]:
    """准备 →（直接落库完成准备）→ 人工确认 → 授权，返回 (prepared_digest, authorization_id)。"""
    r = client.post(
        f"/api/v1/tasks/{task_id}/prepare", json={"revision": 1}, headers={**EXECUTOR_HEADERS, **ik()}
    )
    assert r.status_code == 202, r.text
    preparation_id = r.json()["preparation_id"]

    # 执行链（Agent E）尚未交付：测试直接经服务函数落库"准备完成"，
    # prepared_digest 由真实 compute_prepared_digest 计算（非补造）
    from dsh_sim.api.services.prep_service import mark_preparation_ready

    factory = client.app.state.session_factory
    s = factory()
    prep = mark_preparation_ready(
        s,
        preparation_id,
        prepared_artifacts={"prepared_A.sim": "1" * 64, "prepared_B.sim": "2" * 64},
        readback_sha256="3" * 64,
        adapter_build="mock-adapter-0.1.0",
        software_build="UNCONFIRMED",
    )
    s.commit()
    s.close()
    prepared_digest = prep.prepared_digest

    # 人工确认凭据（受信 UI 会话）
    r = client.post(
        "/api/v1/confirmations",
        json={"action": "authorizeRuns", "target_id": task_id, "target_digest": prepared_digest},
        headers={**EXECUTOR_HEADERS, **ik()},
    )
    assert r.status_code == 201, r.text
    confirmation_id = r.json()["confirmation_id"]

    r = client.post(
        f"/api/v1/tasks/{task_id}/authorizations",
        json={
            "revision": 1,
            "preparation_id": preparation_id,
            "prepared_digest": prepared_digest,
            "execution_budget": make_spec()["execution_budget"],
            "confirmation_id": confirmation_id,
        },
        headers={**EXECUTOR_HEADERS, **ik()},
    )
    assert r.status_code == 201, r.text
    return prepared_digest, r.json()["authorization_id"]


class TestTaskAndHash:
    def test_create_task_draft_blockers(self, client):
        r = client.post(
            "/api/v1/tasks",
            json={
                "draft": {
                    "purpose": "design_screening",
                    "open_questions": [
                        {"field": "conditions", "responsible": "eng_zhang", "question": "缺工况表"}
                    ],
                }
            },
            headers={**EXECUTOR_HEADERS, **ik()},
        )
        assert r.status_code == 201
        body = r.json()
        assert body["task_state"] == "DRAFT"
        assert body["blockers"][0]["responsible"] == "eng_zhang"

    def test_spec_sha256_stable_and_matches_canonical(self, client):
        """哈希集成：createRevision → spec_sha256 与服务侧 canonical 计算一致。"""
        spec = make_spec()
        _, digest = _create_task_with_revision(client, spec)
        expected = spec_sha256(
            TaskSpec.model_validate(spec).model_dump(mode="json", exclude_none=True)
        )
        assert digest == expected

    def test_revision_change_produces_new_digest(self, client):
        """修订变化 → 新摘要；且旧授权/包失效由新修订触发（FR-23 另测）。"""
        task_id, digest1 = _create_task_with_revision(client)
        spec2 = make_spec()
        spec2["conditions"][0]["fields"][0]["quantity"]["si_value"] = 102000.0
        r = client.post(
            f"/api/v1/tasks/{task_id}/revisions",
            json={"expected_revision": 1, "spec": spec2},
            headers={**EXECUTOR_HEADERS, **ik()},
        )
        assert r.status_code == 201, r.text
        assert r.json()["spec_sha256"] != digest1
        assert r.json()["revision"] == 2

    def test_expected_revision_conflict(self, client):
        task_id, _ = _create_task_with_revision(client)
        r = client.post(
            f"/api/v1/tasks/{task_id}/revisions",
            json={"expected_revision": 0, "spec": make_spec()},
            headers={**EXECUTOR_HEADERS, **ik()},
        )
        assert r.status_code == 409
        assert r.json()["code"] == "CONFLICT_REVISION"


class TestIdempotency:
    def test_same_key_replay_returns_same_resource(self, client):
        key = ik()
        body = {"draft": {"purpose": "design_screening"}}
        r1 = client.post("/api/v1/tasks", json=body, headers={**EXECUTOR_HEADERS, **key})
        r2 = client.post("/api/v1/tasks", json=body, headers={**EXECUTOR_HEADERS, **key})
        assert r1.status_code == r2.status_code == 201
        assert r1.json()["task_id"] == r2.json()["task_id"]

    def test_same_key_different_body_409(self, client):
        key = ik()
        client.post(
            "/api/v1/tasks",
            json={"draft": {"purpose": "design_screening"}},
            headers={**EXECUTOR_HEADERS, **key},
        )
        r = client.post(
            "/api/v1/tasks",
            json={
                "draft": {
                    "purpose": "design_screening",
                    "open_questions": [
                        {"field": "variants", "responsible": "x", "question": "q"}
                    ],
                }
            },
            headers={**EXECUTOR_HEADERS, **key},
        )
        assert r.status_code == 409
        assert r.json()["code"] == "CONFLICT_IDEMPOTENCY"

    def test_missing_idempotency_key_rejected(self, client):
        r = client.post(
            "/api/v1/tasks",
            json={"draft": {"purpose": "design_screening"}},
            headers=EXECUTOR_HEADERS,
        )
        assert r.status_code == 422
        assert r.json()["code"] == "VALIDATION"


class TestPrepareSubmitFlow:
    def test_prepare_creates_blocked_preparation(self, client):
        """prepare 状态迁移真实落库；STAR 执行链为 Agent E 范围，blockers 显式 BLOCKED。"""
        task_id, _ = _create_task_with_revision(client)
        r = client.post(
            f"/api/v1/tasks/{task_id}/prepare", json={"revision": 1}, headers={**EXECUTOR_HEADERS, **ik()}
        )
        assert r.status_code == 202
        preparation_id = r.json()["preparation_id"]
        r = client.get(f"/api/v1/preparations/{preparation_id}", headers=EXECUTOR_HEADERS)
        assert r.status_code == 200
        assert r.json()["blockers"][0]["kind"] == "BLOCKED"

    def test_submit_runs_matrix_and_replay(self, client):
        task_id, _ = _create_task_with_revision(client)
        prepared_digest, auth_id = _prepare_and_authorize(client, task_id)
        key = ik()
        r1 = client.post(
            f"/api/v1/tasks/{task_id}/submissions",
            json={"authorization_id": auth_id, "prepared_digest": prepared_digest},
            headers={**EXECUTOR_HEADERS, **key},
        )
        assert r1.status_code == 202, r1.text
        run_ids = r1.json()["run_ids"]
        assert len(run_ids) == 2  # 2 variants × 1 condition
        # 超时重发：同一 Run 集合，不重复创建
        r2 = client.post(
            f"/api/v1/tasks/{task_id}/submissions",
            json={"authorization_id": auth_id, "prepared_digest": prepared_digest},
            headers={**EXECUTOR_HEADERS, **key},
        )
        assert r2.json()["run_ids"] == run_ids
        r = client.get(f"/api/v1/runs/{run_ids[0]}", headers=EXECUTOR_HEADERS)
        assert r.json()["execution_state"] == "QUEUED"
        assert r.json()["numerical_state"] == "NOT_CHECKED"
        assert r.json()["applicability_state"] == "UNCONFIRMED"

    def test_submit_runs_digest_mismatch_409(self, client):
        task_id, _ = _create_task_with_revision(client)
        prepared_digest, auth_id = _prepare_and_authorize(client, task_id)
        r = client.post(
            f"/api/v1/tasks/{task_id}/submissions",
            json={"authorization_id": auth_id, "prepared_digest": "0" * 64},
            headers={**EXECUTOR_HEADERS, **ik()},
        )
        assert r.status_code == 409
        assert r.json()["code"] == "CONFLICT_DIGEST"


class TestPermissions:
    def test_unauthenticated_401(self, client):
        r = client.post("/api/v1/tasks", json={"draft": {"purpose": "design_screening"}}, headers=ik())
        assert r.status_code == 401
        assert r.json()["code"] == "UNAUTHORIZED"

    def test_agent_authorize_runs_rejected(self, client):
        """强约束：Agent Bearer 调 authorizeRuns 永远 403。"""
        task_id, _ = _create_task_with_revision(client)
        r = client.post(
            f"/api/v1/tasks/{task_id}/authorizations",
            json={
                "revision": 1,
                "preparation_id": "prep_x",
                "prepared_digest": "0" * 64,
                "execution_budget": {},
                "confirmation_id": "conf_x",
            },
            headers={**AGENT_HEADERS, **ik()},
        )
        assert r.status_code == 403
        assert r.json()["code"] == "FORBIDDEN"

    def test_agent_confirmation_rejected(self, client):
        r = client.post(
            "/api/v1/confirmations",
            json={"action": "decideReview", "target_id": "x", "target_digest": "0" * 64},
            headers={**AGENT_HEADERS, **ik()},
        )
        assert r.status_code == 403

    def test_cross_project_read_rejected(self, client):
        """跨项目读取被拒（FR-24）。"""
        task_id, _ = _create_task_with_revision(client)
        prepared_digest, auth_id = _prepare_and_authorize(client, task_id)
        r = client.post(
            f"/api/v1/tasks/{task_id}/submissions",
            json={"authorization_id": auth_id, "prepared_digest": prepared_digest},
            headers={**EXECUTOR_HEADERS, **ik()},
        )
        run_id = r.json()["run_ids"][0]
        r = client.get(f"/api/v1/runs/{run_id}", headers=OTHER_PROJECT_HEADERS)
        assert r.status_code == 403
        assert r.json()["code"] == "FORBIDDEN"


class TestReviewFlow:
    def _setup_review(self, client) -> tuple[str, str, str]:
        """完整链路到 IN_REVIEW，返回 (task_id, review_id, bundle_digest)。"""
        task_id, _ = _create_task_with_revision(client)
        prepared_digest, auth_id = _prepare_and_authorize(client, task_id)
        client.post(
            f"/api/v1/tasks/{task_id}/submissions",
            json={"authorization_id": auth_id, "prepared_digest": prepared_digest},
            headers={**EXECUTOR_HEADERS, **ik()},
        )
        bundle_digest = "d" * 64
        s = client.app.state.session_factory()
        s.add(
            BundleRow(
                bundle_id="bnd_test1",
                task_id=task_id,
                revision=1,
                bundle_digest=bundle_digest,
                manifest=[],
                validity="CURRENT",
            )
        )
        s.commit()
        s.close()
        r = client.post(
            f"/api/v1/tasks/{task_id}/reviews",
            json={
                "revision": 1,
                "bundle_id": "bnd_test1",
                "bundle_digest": bundle_digest,
                "reviewer_id": REVIEWER_HEADERS["X-Dev-Subject"],
            },
            headers={**EXECUTOR_HEADERS, **ik()},
        )
        assert r.status_code == 201, r.text
        return task_id, r.json()["review_id"], bundle_digest

    def test_submit_review_transitions(self, client):
        _, review_id, _ = self._setup_review(client)
        r = client.get(f"/api/v1/reviews/{review_id}", headers=REVIEWER_HEADERS)
        assert r.json()["state"] == "PENDING"

    def test_agent_decide_rejected(self, client):
        _, review_id, bundle_digest = self._setup_review(client)
        r = client.post(
            f"/api/v1/reviews/{review_id}/decisions",
            json={
                "outcome": "ACCEPT",
                "bundle_digest": bundle_digest,
                "revision": 1,
                "confirmation_id": "conf_x",
            },
            headers={**AGENT_HEADERS, **ik()},
        )
        assert r.status_code == 403
        assert r.json()["code"] == "FORBIDDEN"

    def test_executor_decide_own_task_rejected(self, client):
        """职责分离：执行者 decideReview 自己被拒（FR-22 不可自行验收）。"""
        _, review_id, bundle_digest = self._setup_review(client)
        r = client.post(
            f"/api/v1/reviews/{review_id}/decisions",
            json={
                "outcome": "ACCEPT",
                "bundle_digest": bundle_digest,
                "revision": 1,
                "confirmation_id": "conf_x",
            },
            headers={**EXECUTOR_HEADERS, **ik()},
        )
        assert r.status_code == 403
        assert r.json()["code"] == "FORBIDDEN"

    def test_accept_blocked_when_runs_incomplete(self, client):
        """存在未完成 Run / UNCONFIRMED 时 ACCEPT 必须失败（仍允许 REQUEST_CHANGES）。"""
        _, review_id, bundle_digest = self._setup_review(client)
        r = client.post(
            f"/api/v1/reviews/{review_id}/decisions",
            json={
                "outcome": "ACCEPT",
                "bundle_digest": bundle_digest,
                "revision": 1,
                "confirmation_id": "conf_x",
            },
            headers={**REVIEWER_HEADERS, **ik()},
        )
        assert r.status_code == 503
        body = r.json()
        assert body["code"] == "BLOCKED"
        assert body["retryable"] is False

    def test_request_changes_happy_path(self, client):
        _, review_id, bundle_digest = self._setup_review(client)
        r = client.post(
            "/api/v1/confirmations",
            json={"action": "decideReview", "target_id": review_id, "target_digest": bundle_digest},
            headers={**REVIEWER_HEADERS, **ik()},
        )
        confirmation_id = r.json()["confirmation_id"]
        r = client.post(
            f"/api/v1/reviews/{review_id}/decisions",
            json={
                "outcome": "REQUEST_CHANGES",
                "bundle_digest": bundle_digest,
                "revision": 1,
                "limitations": "补算工况 C1 后重报",
                "confirmation_id": confirmation_id,
            },
            headers={**REVIEWER_HEADERS, **ik()},
        )
        assert r.status_code == 201, r.text
        assert r.json()["outcome"] == "REQUEST_CHANGES"
        r = client.get(f"/api/v1/reviews/{review_id}", headers=REVIEWER_HEADERS)
        assert r.json()["state"] == "CHANGES_REQUESTED"

    def test_build_bundle_blocked_placeholder(self, client):
        """STAR 执行/证据冻结链占位：显式 BLOCKED，不实现 Mock 执行链。"""
        task_id, _ = _create_task_with_revision(client)
        r = client.post(
            f"/api/v1/tasks/{task_id}/bundles",
            json={"revision": 1},
            headers={**EXECUTOR_HEADERS, **ik()},
        )
        assert r.status_code == 503
        assert r.json()["code"] == "BLOCKED"
        assert r.json()["retryable"] is False


class TestJobEndpoints:
    def test_claim_and_post_event(self, client):
        task_id, _ = _create_task_with_revision(client)
        client.post(
            f"/api/v1/tasks/{task_id}/prepare", json={"revision": 1}, headers={**EXECUTOR_HEADERS, **ik()}
        )
        r = client.post(
            "/api/v1/jobs/claim",
            json={"node_id": "node_1", "node_capabilities": {"star_build": "UNCONFIRMED"}},
            headers={**NODE_HEADERS, **ik()},
        )
        assert r.status_code == 200, r.text
        job, lease = r.json()["job"], r.json()["lease"]
        assert job["kind"] == "PREPARE"
        assert lease["fencing_token"] == 1
        r = client.post(
            f"/api/v1/jobs/{job['job_id']}/events",
            json={
                "lease_id": lease["lease_id"],
                "fencing_token": lease["fencing_token"],
                "event_seq": 1,
                "kind": "STARTING",
                "payload": {"attempt_id": job["attempt_id"], "work_dir_digest": "0" * 64},
            },
            headers={**NODE_HEADERS, **ik()},
        )
        assert r.status_code == 202
        assert r.json()["accepted"] is True

    def test_claim_requires_node_role(self, client):
        r = client.post(
            "/api/v1/jobs/claim",
            json={"node_id": "node_1", "node_capabilities": {}},
            headers={**EXECUTOR_HEADERS, **ik()},
        )
        assert r.status_code == 403


class TestArtifactFlow:
    def test_register_upload_download(self, client):
        task_id, _ = _create_task_with_revision(client)
        client.post(
            f"/api/v1/tasks/{task_id}/prepare", json={"revision": 1}, headers={**EXECUTOR_HEADERS, **ik()}
        )
        r = client.post(
            "/api/v1/jobs/claim",
            json={"node_id": "node_1", "node_capabilities": {}},
            headers={**NODE_HEADERS, **ik()},
        )
        job = r.json()["job"]
        content = b"monitor csv content"
        sha = hashlib.sha256(content).hexdigest()
        r = client.post(
            "/api/v1/artifacts",
            json={
                "job_id": job["job_id"],
                "logical_path": "runs/run1/monitor.csv",
                "length": len(content),
                "sha256": sha,
            },
            headers={**NODE_HEADERS, "X-Dev-Node-Id": "node_1", **ik()},
        )
        assert r.status_code == 201, r.text
        artifact_id = r.json()["artifact_id"]

        # 摘要不符 → 409
        r = client.put(
            f"/api/v1/artifacts/{artifact_id}/content",
            content=b"tampered",
            headers={**NODE_HEADERS, "X-Dev-Node-Id": "node_1"},
        )
        assert r.status_code == 409

        # 正确上传 → COMMITTED
        r = client.put(
            f"/api/v1/artifacts/{artifact_id}/content",
            content=content,
            headers={**NODE_HEADERS, "X-Dev-Node-Id": "node_1"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["state"] == "COMMITTED"

        # 跨项目读取被拒
        r = client.get(f"/api/v1/artifacts/{artifact_id}/content", headers=OTHER_PROJECT_HEADERS)
        assert r.status_code == 403
        r = client.get(f"/api/v1/artifacts/{artifact_id}/content", headers=EXECUTOR_HEADERS)
        assert r.status_code == 200
        assert r.content == content


class TestCapabilities:
    def test_list_capabilities_empty(self, client):
        r = client.get("/api/v1/capabilities", headers=EXECUTOR_HEADERS)
        assert r.status_code == 200
        assert r.json()["items"] == []


class TestEventsIncremental:
    """GET /tasks/{task_id}/events：跨 job 合并、(occurred_at,event_seq) 排序、
    global_seq 重编号、after_seq 增量、跨项目 403。"""

    def _two_jobs_three_events(self, client):
        """造 1 任务 × 2 job（各 3 事件），返回 (task_id, job_ids)。"""
        task_id, _ = _create_task_with_revision(client)
        s = client.app.state.session_factory()
        from dsh_sim.db.models import EventRow, JobRow
        from dsh_sim.db.models import utcnow
        from datetime import timedelta

        base = utcnow()
        job_ids = []
        try:
            for j in range(2):
                job = JobRow(job_id=f"job_ev{j}", kind="EXECUTE", task_id=task_id)
                s.add(job)
                s.flush()
                job_ids.append(job.job_id)
                for seq in range(1, 4):
                    s.add(
                        EventRow(
                            event_id=f"evt_{j}_{seq}",
                            job_id=job.job_id,
                            event_seq=seq,
                            kind="RUNNING",
                            payload={"job": j, "seq": seq},
                            occurred_at=base + timedelta(seconds=j * 10 + seq),
                        )
                    )
                    s.flush()
            s.commit()
        finally:
            s.close()
        return task_id, job_ids

    def test_full_pull_order_and_global_seq(self, client):
        task_id, job_ids = self._two_jobs_three_events(client)
        r = client.get(f"/api/v1/tasks/{task_id}/events", headers=EXECUTOR_HEADERS)
        assert r.status_code == 200, r.text
        body = r.json()
        items = body["items"]
        assert len(items) == 6  # 2 job × 3 事件，跨 job 合并
        # global_seq 从 1 连续递增；occurred_at 全局有序
        assert [i["global_seq"] for i in items] == [1, 2, 3, 4, 5, 6]
        assert [i["occurred_at"] for i in items] == sorted(i["occurred_at"] for i in items)
        # 两个 job 的事件都在
        assert {i["job_id"] for i in items} == set(job_ids)
        assert body["next_after"] == 6

    def test_after_seq_incremental(self, client):
        task_id, _ = self._two_jobs_three_events(client)
        full = client.get(f"/api/v1/tasks/{task_id}/events", headers=EXECUTOR_HEADERS).json()
        next_after = full["next_after"]
        # 无新事件：增量为空、next_after 不变
        r = client.get(
            f"/api/v1/tasks/{task_id}/events?after_seq={next_after}",
            headers=EXECUTOR_HEADERS,
        )
        assert r.status_code == 200
        assert r.json()["items"] == []
        assert r.json()["next_after"] == next_after
        # 追加 1 条新事件后：增量只回新的
        s = client.app.state.session_factory()
        from dsh_sim.db.models import EventRow
        from dsh_sim.db.models import utcnow
        from datetime import timedelta

        try:
            s.add(
                EventRow(
                    event_id="evt_new_1",
                    job_id="job_ev1",
                    event_seq=4,
                    kind="COMPLETED",
                    payload={"exit_code": 0},
                    occurred_at=utcnow() + timedelta(seconds=99),
                )
            )
            s.commit()
        finally:
            s.close()
        r = client.get(
            f"/api/v1/tasks/{task_id}/events?after_seq={next_after}",
            headers=EXECUTOR_HEADERS,
        )
        items = r.json()["items"]
        assert len(items) == 1
        assert items[0]["kind"] == "COMPLETED"
        assert r.json()["next_after"] > next_after

    def test_cross_project_403(self, client):
        task_id, _ = self._two_jobs_three_events(client)
        r = client.get(f"/api/v1/tasks/{task_id}/events", headers=OTHER_PROJECT_HEADERS)
        assert r.status_code == 403
        assert r.json()["code"] == "FORBIDDEN"
