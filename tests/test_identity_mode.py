"""上游验收报告问题 5（2026-09-22）：开发身份不能充当多人审批身份。

验收条件 → 本文件四组测试：
1. 显式本地开发模式：只有显式声明 dev/development/local 才接受 X-Dev-* 自报头；
2. 非开发环境默认拒绝 X-Dev：未设置、空值、无法识别的取值与显式生产模式一律 401；
3. 拒绝发生在**信任输入之前**（不依赖角色判断兜底），诊断信息说明当前模式与建议；
4. 界面角色下拉框不是安全边界：即使在开发模式下，AGENT 身份仍不能取得人工批准动作。

身份解析是 fail-closed 的：本文件用真实 app 实例（不同 identity_mode）逐一验证。
"""
from __future__ import annotations

import pytest
from conftest import AGENT_HEADERS, EXECUTOR_HEADERS
from fastapi.testclient import TestClient

from dsh_sim.api.deps import DEV_MODE_VALUES, IDENTITY_MODE_ENV, resolve_identity_mode
from dsh_sim.api.main import create_app

pytestmark = pytest.mark.mock


def _app(tmp_path, **kwargs) -> TestClient:
    app = create_app(
        database_url=f"sqlite:///{(tmp_path / 'id.db').as_posix()}",
        artifact_root=tmp_path / "artifacts",
        **kwargs,
    )
    return TestClient(app)


# ---------------------------------------------------------------------------
# 条件 1/2：显式开发模式 vs 默认拒绝
# ---------------------------------------------------------------------------
class TestIdentityModeResolution:
    @pytest.mark.parametrize("raw", ["dev", "DEV", " development ", "Local", "local"])
    def test_recognises_explicit_dev_values(self, raw):
        assert resolve_identity_mode(raw) == "dev"

    @pytest.mark.parametrize(
        "raw", [None, "", "   ", "prod", "production", "staging", "yes", "1", "dev2", "dev,prod"]
    )
    def test_everything_else_is_prod(self, raw):
        """fail-closed：只有白名单取值是 dev，其余（含未设置/空/生产/无法识别）都是 prod。"""
        assert resolve_identity_mode(raw) == "prod"

    def test_dev_whitelist_is_small_and_explicit(self):
        assert DEV_MODE_VALUES == {"dev", "development", "local"}


class TestDefaultRejectsDevHeaders:
    def test_env_unset_defaults_to_prod_and_rejects(self, tmp_path, monkeypatch):
        """未显式声明开发模式 → 生产模式 → 完整的 X-Dev 头也被拒绝。"""
        monkeypatch.delenv(IDENTITY_MODE_ENV, raising=False)
        with _app(tmp_path) as client:
            r = client.get("/api/v1/capabilities?limit=1", headers=EXECUTOR_HEADERS)
        assert r.status_code == 401, r.text
        body = r.json()
        assert body["code"] == "UNAUTHORIZED"
        assert body["details"]["identity_mode"] == "prod"
        assert body["details"]["dev_headers_present"] is True
        assert IDENTITY_MODE_ENV in body["details"]["hint"]

    def test_no_identity_at_all_is_also_rejected(self, tmp_path, monkeypatch):
        monkeypatch.delenv(IDENTITY_MODE_ENV, raising=False)
        with _app(tmp_path) as client:
            r = client.get("/api/v1/capabilities?limit=1")
        assert r.status_code == 401, r.text

    def test_explicit_prod_rejects_complete_headers(self, tmp_path):
        with _app(tmp_path, identity_mode="prod") as client:
            r = client.get("/api/v1/capabilities?limit=1", headers=EXECUTOR_HEADERS)
        assert r.status_code == 401, r.text
        assert r.json()["details"]["identity_mode"] == "prod"
        assert "TBD-08" in r.json()["message"]

    @pytest.mark.parametrize("mode", ["", "  ", "staging", "production"])
    def test_unrecognised_modes_behave_like_prod(self, tmp_path, mode):
        with _app(tmp_path, identity_mode=mode) as client:
            r = client.get("/api/v1/capabilities?limit=1", headers=EXECUTOR_HEADERS)
        assert r.status_code == 401, r.text


class TestExplicitDevModeAccepts:
    def test_dev_via_constructor(self, tmp_path):
        with _app(tmp_path, identity_mode="dev") as client:
            r = client.get("/api/v1/capabilities?limit=1", headers=EXECUTOR_HEADERS)
        assert r.status_code == 200, r.text

    def test_dev_via_env_var(self, tmp_path, monkeypatch):
        """显式环境变量声明本地开发模式（本地/隔离环境的标准做法）。"""
        monkeypatch.setenv(IDENTITY_MODE_ENV, "dev")
        with _app(tmp_path) as client:
            r = client.get("/api/v1/capabilities?limit=1", headers=EXECUTOR_HEADERS)
        assert r.status_code == 200, r.text

    def test_constructor_argument_wins_over_env(self, tmp_path, monkeypatch):
        """显式传参优先于环境变量：容器/测试可以固定模式，不受外部环境污染。"""
        monkeypatch.setenv(IDENTITY_MODE_ENV, "dev")
        with _app(tmp_path, identity_mode="prod") as client:
            r = client.get("/api/v1/capabilities?limit=1", headers=EXECUTOR_HEADERS)
        assert r.status_code == 401, r.text

    def test_dev_mode_requires_subject_header(self, tmp_path):
        """开发模式也不是"无身份"：缺 X-Dev-Subject 仍 401。"""
        with _app(tmp_path, identity_mode="dev") as client:
            r = client.get("/api/v1/capabilities?limit=1", headers={"X-Dev-Roles": "REVIEWER"})
        assert r.status_code == 401, r.text
        assert "X-Dev-Subject" in r.json()["message"]


# ---------------------------------------------------------------------------
# 条件 3/4：拒绝先于信任输入；角色下拉框不是安全边界
# ---------------------------------------------------------------------------
class TestRoleIsNotASecurityBoundary:
    def test_agent_cannot_take_human_action_even_in_dev_mode(self, tmp_path):
        """开发模式下自报 REVIEWER 会话也不能让 AGENT 取得人工确认动作。"""
        with _app(tmp_path, identity_mode="dev") as client:
            r = client.post(
                "/api/v1/confirmations",
                json={
                    "action": "authorizeRuns",
                    "target_id": "task_whatever",
                    "target_digest": "0" * 64,
                },
                headers={**AGENT_HEADERS, "Idempotency-Key": "k" * 16},
            )
        assert r.status_code == 403, r.text
        assert "Agent" in r.json()["message"]

    def test_prod_rejection_does_not_depend_on_role_claims(self, tmp_path):
        """生产模式的拒绝不看角色：连 AGENT 自报身份也一并在信任输入前拒绝。"""
        with _app(tmp_path, identity_mode="prod") as client:
            r = client.post(
                "/api/v1/confirmations",
                json={
                    "action": "authorizeRuns",
                    "target_id": "task_whatever",
                    "target_digest": "0" * 64,
                },
                headers={**AGENT_HEADERS, "Idempotency-Key": "k" * 16},
            )
        assert r.status_code == 401, r.text
        assert r.json()["code"] == "UNAUTHORIZED"


class TestSeparationDimensionsStillEnforced:
    """报告条件「项目、Worker 节点和执行/审查职责分别校验」：在开发模式下逐维验证。"""

    def test_worker_endpoint_requires_node_admin(self, tmp_path):
        with _app(tmp_path, identity_mode="dev") as client:
            r = client.post(
                "/api/v1/jobs/claim",
                json={"node_id": "node-x"},
                headers={**EXECUTOR_HEADERS, "Idempotency-Key": "k" * 16},
            )
        assert r.status_code == 403, r.text
        assert "NODE_ADMIN" in r.json()["message"]

    def test_cross_project_access_denied(self, tmp_path):
        from conftest import OTHER_PROJECT_HEADERS

        with _app(tmp_path, identity_mode="dev") as client:
            r = client.post(
                "/api/v1/tasks",
                json={"draft": {"purpose": "design_screening"}},
                headers={**EXECUTOR_HEADERS, "Idempotency-Key": "k" * 16},
            )
            assert r.status_code == 201, r.text
            task_id = r.json()["task_id"]
            r = client.get(f"/api/v1/tasks/{task_id}", headers=OTHER_PROJECT_HEADERS)
        assert r.status_code == 403, r.text
