"""WP-11 MCP 12 工具测试（mock 层，Agent G）。

测试边界：MCP 工具 ↔ 工程 API 的受限投影正确性（AGENT 身份头、幂等键、
错误模型透传、UNAVAILABLE 结构化错误）。经 ASGI transport 打真实 FastAPI app
（SQLite tmp 库），不起端口、不需网络、不需 STAR-CCM+。

如实标注：
- get_task 依赖 GET /tasks/{task_id}：契约意图内（openapi 映射表见
  docs/mcp-integration.md），但 Agent C 服务路由尚未暴露该只读端点
  （api/ 目录属 Agent C/E 边界，本文件不改）。测试 app 显式补挂该路由以验证
  MCP 投影行为；真实服务缺口在 docs 与简报中标注。
- build_bundle：服务显式 503 BLOCKED（Agent E evidence/ 未交付，见
  review_service.build_bundle）。测试断言 BLOCKED 错误如实透传，不补造 manifest。
"""
from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import httpx
import pytest
from fastapi import Depends
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from dsh_sim.api.deps import get_identity, get_session
from dsh_sim.api.main import create_app
from dsh_sim.api.services.task_service import get_task_row
from dsh_sim.db.models import BundleRow, CapabilityPackageRow, ReviewRow
from dsh_sim.domain.identity import Identity
from dsh_sim.domain.schemas import Task

# fastmcp 仅装在 dsh-sim 独立 venv（envs/dsh-sim）；共享 default env 没有。
# importorskip 让全量跑在此优雅跳过，而非收集崩溃（独立 venv 中 11 条全跑）。
fastmcp = pytest.importorskip("fastmcp", reason="fastmcp 仅在 envs/dsh-sim venv")
from dsh_sim.mcp import server  # noqa: E402  (依赖 fastmcp 存在后才导入)
Client = fastmcp.Client

from conftest import EXECUTOR_HEADERS, make_spec

pytestmark = pytest.mark.mock

AGENT_HEADERS = {
    "X-Dev-Subject": "agent-dsh-sim",
    "X-Dev-Roles": "AGENT",
    "X-Dev-Projects": "proj_a",
}


def ik() -> dict[str, str]:
    return {"Idempotency-Key": uuid.uuid4().hex * 2}


def _mount_get_task(app) -> None:
    """补挂 GET /api/v1/tasks/{task_id}（契约缺口；见模块 docstring 如实标注）。"""

    @app.get("/api/v1/tasks/{task_id}", operation_id="getTask")
    def _get_task(
        task_id: str,
        session: Session = Depends(get_session),
        identity: Identity = Depends(get_identity),
    ) -> JSONResponse:
        row = get_task_row(session, identity, task_id)
        task = Task(
            task_id=row.task_id,
            project_id=row.project_id,
            current_revision=row.current_revision,
            task_state=row.task_state,
            review_state=row.review_state,
            owner_id=row.owner_id,
            purpose=row.purpose,
            blockers=row.blockers,
            created_at=row.created_at,
        )
        return JSONResponse(task.model_dump(mode="json"))


@pytest.fixture()
def api(tmp_path, monkeypatch) -> SimpleNamespace:
    """真实 FastAPI app（tmp SQLite）+ AGENT 身份的 ASGI client 注入 MCP server。"""
    app = create_app(
        database_url=f"sqlite:///{(tmp_path / 'mcp.db').as_posix()}",
        artifact_root=tmp_path / "artifacts",
    )
    _mount_get_task(app)
    agent_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver/api/v1",
        headers=AGENT_HEADERS,
        timeout=httpx.Timeout(60.0),
    )
    monkeypatch.setattr(server, "_client", agent_client)
    with TestClient(app) as setup_client:
        yield SimpleNamespace(app=app, setup=setup_client)


def call(tool: str, **args):
    """经 fastmcp Client 直连 server 调工具，返回 structured data。"""

    async def _go():
        async with Client(server.mcp) as c:
            result = await c.call_tool(tool, args)
            return result.data

    return asyncio.run(_go())


# ---------------------------------------------------------------------------
# 造数 helper（业务流转经 MCP 工具 + EXECUTOR 人工段经 TestClient）
# ---------------------------------------------------------------------------


def _task_with_revision() -> str:
    t = call("create_task", draft={"purpose": "design_screening"})
    assert "task_id" in t, t
    r = call("revise_task", task_id=t["task_id"], expected_revision=0, spec=make_spec())
    assert r.get("revision") == 1, r
    return t["task_id"]


def _prepare_ready_and_authorize(api, task_id: str) -> tuple[str, str, str]:
    """MCP prepare_task → 服务函数落库 ready（同 test_api_contract 模式）→
    EXECUTOR 人工确认 + 授权。返回 (preparation_id, prepared_digest, authorization_id)。"""
    p = call("prepare_task", task_id=task_id, revision=1)
    assert "preparation_id" in p, p
    preparation_id = p["preparation_id"]

    from dsh_sim.api.services.prep_service import mark_preparation_ready

    s = api.app.state.session_factory()
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

    r = api.setup.post(
        "/api/v1/confirmations",
        json={"action": "authorizeRuns", "target_id": task_id, "target_digest": prepared_digest},
        headers={**EXECUTOR_HEADERS, **ik()},
    )
    assert r.status_code == 201, r.text
    r = api.setup.post(
        f"/api/v1/tasks/{task_id}/authorizations",
        json={
            "revision": 1,
            "preparation_id": preparation_id,
            "prepared_digest": prepared_digest,
            "execution_budget": make_spec()["execution_budget"],
            "confirmation_id": r.json()["confirmation_id"],
        },
        headers={**EXECUTOR_HEADERS, **ik()},
    )
    assert r.status_code == 201, r.text
    return preparation_id, prepared_digest, r.json()["authorization_id"]


def _make_bundle_and_review(api, task_id: str) -> tuple[str, str]:
    """DB 直接造 BundleRow + ReviewRow（证据链属 Agent E；此处仅为查询类工具造合法输入）。"""
    s = api.app.state.session_factory()
    bundle = BundleRow(
        bundle_id="bun_test01",
        task_id=task_id,
        revision=1,
        bundle_digest="d" * 64,
        manifest=[
            {"artifact_id": "art_1", "logical_path": "reports/A_C1.csv", "sha256": "e" * 64},
            {"artifact_id": "art_2", "logical_path": "results/B_C1.sim", "sha256": "f" * 64},
        ],
        validity="CURRENT",
    )
    review = ReviewRow(
        review_id="rev_test01",
        task_id=task_id,
        revision=1,
        bundle_id="bun_test01",
        bundle_digest="d" * 64,
        reviewer_id="rev_li",
        state="PENDING",
        validity="CURRENT",
    )
    s.add_all([bundle, review])
    s.commit()
    s.close()
    return bundle.bundle_id, review.review_id


# ---------------------------------------------------------------------------
# 工具清单自检
# ---------------------------------------------------------------------------


def test_tool_manifest_selfcheck():
    """list_tools == CONVENTIONS §3.5 的 12 个名字（一字不差）；自检无禁止工具。"""

    async def _go():
        async with Client(server.mcp) as c:
            return sorted(t.name for t in await c.list_tools())

    names = asyncio.run(_go())
    assert names == sorted(server.EXPECTED_TOOLS)
    assert server.selfcheck(names) == []
    # main() 启动自检同路径（fastmcp server.list_tools），防回归
    assert asyncio.run(server._registered_tool_names()) == names


# ---------------------------------------------------------------------------
# 12 工具 happy path（服务当前能力真相；build_bundle 为如实 BLOCKED）
# ---------------------------------------------------------------------------


def test_list_capabilities(api):
    # 应用启动时注册器已把磁盘 buffer_chamber/0.1.0 注册为 DRAFT（WP-17）。
    # 这里模拟人工发布：UPDATE 既有行 → RELEASED（不再 INSERT，注册器已占位）。
    s = api.app.state.session_factory()
    row = (
        s.query(CapabilityPackageRow)
        .filter_by(capability_package_id="buffer_chamber", version="0.1.0")
        .one_or_none()
    )
    if row is None:
        s.add(
            CapabilityPackageRow(
                capability_package_id="buffer_chamber",
                version="0.1.0",
                manifest_sha256="c" * 64,
                status="RELEASED",
                purpose="design_screening",
                domain_summary="缓冲腔任务族（mock 登记）",
                compatibility=None,
            )
        )
    else:
        row.status = "RELEASED"
    s.commit()
    s.close()
    body = call("list_capabilities")
    assert "items" in body, body
    assert any(i["capability_package_id"] == "buffer_chamber" for i in body["items"])


def test_create_task_and_get_task(api):
    t = call("create_task", draft={"purpose": "design_screening"})
    assert t["task_state"] == "DRAFT", t
    g = call("get_task", task_id=t["task_id"])
    assert g["task_id"] == t["task_id"], g
    assert g["task_state"] == "DRAFT"


def test_create_task_idempotent_same_args(api):
    """确定性幂等键：同参数重试 → 服务端返回同一任务（CONVENTIONS §3.4）。"""
    draft = {"purpose": "design_screening"}
    t1 = call("create_task", draft=draft)
    t2 = call("create_task", draft=draft)
    assert t1["task_id"] == t2["task_id"]


def test_revise_task(api):
    task_id = _task_with_revision()
    spec2 = make_spec()
    spec2["conditions"][0]["fields"][0]["quantity"]["si_value"] = 102000.0
    r = call("revise_task", task_id=task_id, expected_revision=1, spec=spec2)
    assert r["revision"] == 2, r


def test_prepare_task_and_get_preparation(api):
    task_id = _task_with_revision()
    p = call("prepare_task", task_id=task_id, revision=1)
    assert "preparation_id" in p and "job_id" in p, p
    assert "hint" in p  # 长作业：快速返回 id + 轮询提示
    g = call("get_preparation", preparation_id=p["preparation_id"])
    assert g["preparation_id"] == p["preparation_id"], g
    # 执行链（Agent E）未跑：blockers 如实标记等待 Worker，绝不补造回读
    assert any(b.get("kind") == "BLOCKED" for b in g["blockers"]), g


def test_submit_runs_get_run_cancel_run(api):
    task_id = _task_with_revision()
    _, prepared_digest, authorization_id = _prepare_ready_and_authorize(api, task_id)

    s = call(
        "submit_runs",
        task_id=task_id,
        authorization_id=authorization_id,
        prepared_digest=prepared_digest,
    )
    assert "run_ids" in s, s
    assert len(s["run_ids"]) == 2  # A/B × C1
    assert "hint" in s

    run_id = s["run_ids"][0]
    g = call("get_run", run_id=run_id)
    assert g["run_id"] == run_id, g
    assert g["execution_state"] == "QUEUED"
    assert g["numerical_state"] == "NOT_CHECKED"

    c = call("cancel_run", run_id=run_id, reason="mcp 测试取消")
    assert c["run_id"] == run_id, c
    # 取消语义（queue.service.request_cancel）：QUEUED 无外部进程，现场确认成立 →
    # 直接 CANCELLED；已出租/运行中 → CANCELLING 待 Worker 现场确认。两种都合法。
    assert c["execution_state"] in ("QUEUED", "CANCELLING", "CANCELLED"), c


def test_build_bundle_blocked_passthrough(api):
    """服务显式 BLOCKED（Agent E evidence/ 未交付）：结构化错误如实透传，不补造。"""
    task_id = _task_with_revision()
    body = call("build_bundle", task_id=task_id, revision=1)
    assert body["code"] == "BLOCKED", body
    assert body["retryable"] is False


def test_get_evidence_summary_only(api):
    """只返回摘要 + artifact 引用；manifest 计数与截断标记正确。"""
    task_id = _task_with_revision()
    bundle_id, _ = _make_bundle_and_review(api, task_id)
    body = call("get_evidence", bundle_id=bundle_id)
    assert body["bundle_id"] == bundle_id, body
    assert body["bundle_digest"] == "d" * 64
    assert body["manifest_total"] == 2
    assert body["manifest_truncated"] is False
    entry = body["manifest"][0]
    assert set(entry) == {"artifact_id", "logical_path", "sha256"}  # 引用，非内容


def test_draft_review_issue(api):
    """模型创建的问题永远 DRAFT；转 OPEN 需审查人 confirmIssue（不在工具集中）。"""
    task_id = _task_with_revision()
    _, review_id = _make_bundle_and_review(api, task_id)
    body = call(
        "draft_review_issue",
        review_id=review_id,
        responsible="eng_zhang",
        severity="major",
        description="压损比较口径待确认",
        close_criteria="提供同截面同加权定义证据",
        related_run_ids=["run_x"],
    )
    assert body["status"] == "DRAFT", body
    assert body["created_by"] == "agent-dsh-sim"  # AGENT 身份头已传递
    assert body["issue_id"].startswith("issue_")


# ---------------------------------------------------------------------------
# 服务不可达：UNAVAILABLE 结构化错误（禁止 fallback / 伪造数据）
# ---------------------------------------------------------------------------


def test_unavailable_when_service_down(api, monkeypatch):
    down_client = httpx.AsyncClient(
        base_url="http://127.0.0.1:9/api/v1",  # 无监听端口 → ConnectError
        headers=AGENT_HEADERS,
        timeout=httpx.Timeout(5.0),
        trust_env=False,  # 直连，防系统代理把端口9拦成502（与本机服务语义一致）
    )
    monkeypatch.setattr(server, "_client", down_client)
    body = call("list_capabilities")
    assert body["code"] == "UNAVAILABLE", body
    assert body["message"] == "工程服务不可用"
    assert body["retryable"] is True
    assert body["details"]["source"] == "mcp-bridge"
