"""实机验收断点修复回归（2026-10-01 本地验收轮）。

覆盖三处断点的回归：
- A/B（panels/executor/app.js）：合法角色 EXECUTOR、专用测试项目 proj_a、
  createHumanConfirmation 的 target_id=task_id；面板只授权不提交
  （首次 submit_runs 必须由 AGENT runner 经 MCP 完成）。
- C（worker/service.py）：常驻 worker 在空队列启动后仍能领取后来创建的作业。
- 这些是静态合同/行为回归，不涉及真实求解器（mock 标记）。
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import EXECUTOR_HEADERS, make_spec
from helpers_chain import ik

from dsh_sim.worker.service import StopFlag, idle_poll_seconds, serve

pytestmark = [pytest.mark.mock, pytest.mark.integration]

REPO_ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 断点 A/B：执行台面板与服务端合同一致；授权与提交分离
# ---------------------------------------------------------------------------


def test_executor_panel_uses_valid_role_and_dedicated_project():
    src = _read("panels/executor/app.js")
    assert "roles: 'ENGINEER'" not in src, "旧非法角色 ENGINEER 必须移除（服务端枚举无此值）"
    assert "roles: 'EXECUTOR'" in src, "authorizeRuns 需要 EXECUTOR 角色"
    # 项目不在面板硬编码：shared/api.js 默认 proj_a，显式配置（localStorage/调用方）可覆盖
    assert "projects:" not in src, "面板身份不得硬编码项目，应交给 shared/api.js 默认+覆盖链"
    shared = _read("panels/shared/api.js")
    assert "ls('dshsim.projects', 'proj_a')" in shared, "shared 默认项目必须是 proj_a（专用测试项目）"
    assert "'default'" not in shared.split("devIdentityHeaders")[1].split("}")[0], "旧默认 'default' 必须移除"


def test_reviewer_panel_uses_dedicated_project():
    src = _read("panels/reviewer/app.js")
    assert "projects:" not in src, "审查台同样不得硬编码项目（shared 默认 proj_a 可覆盖）"
    assert "roles: 'REVIEWER'" in src


def test_executor_panel_confirmation_targets_task_id():
    src = _read("panels/executor/app.js")
    # 服务端 consume_confirmation 校验 authorizeRuns 绑定 target_id=task_id；
    # 旧实现传 preparation_id → 403（断点 A 的核心）。
    assert "target_id: t.task_id" in src
    assert "target_id: p.preparation_id" not in src


def test_executor_panel_authorizes_without_submitting():
    src = _read("panels/executor/app.js")
    assert "/submissions" not in src, "执行台不得代 runner 提交作业（授权/提交分离）"
    assert "确认并授权（不提交）" in src, "必须提供仅授权的人工操作"
    assert "submit_runs" in src, "授权结果提示必须指明首次提交由 AGENT runner 经 MCP 完成"


def test_executor_panel_recovers_authorization_without_duplicate():
    """断点返修 2：授权后刷新/关闭弹窗可读回 authorization_id，不重复授权。"""
    src = _read("panels/executor/app.js")
    assert "/authorizations" in src, "必须从受项目权限保护的读取投影取回授权"
    assert "activeAuthorization" in src, "必须计算当前修订有效授权"
    assert "当前有效授权（交接恢复；勿重复授权）" in src
    assert "|| !!activeAuth" in src, "存在有效授权时禁用再次授权按钮"
    assert "不会重复授权" in src, "授权成功弹窗必须说明刷新后可从确认区域读回"
    # 读取投影的响应字段不得包含 confirmation 一次性凭据（行为断言见下方 API 测试；
    # 此处只检查字段构造段没有该键）。
    route = (REPO_ROOT / "src/dsh_sim/api/routes/tasks.py").read_text(encoding="utf-8")
    get_part = route.split("def listTaskAuthorizations")[1].split("@router.post")[0]
    fields_part = get_part.split("items = [")[1]
    assert '"confirmation' not in fields_part, "授权读取投影不得返回 confirmation 凭据字段"


# ---------------------------------------------------------------------------
# 断点 C：常驻 worker 空队列启动后仍能接单
# ---------------------------------------------------------------------------


def _wait_for_log(logs: list[str], needle: str, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if any(needle in line for line in logs):
            return
        time.sleep(0.05)
    raise AssertionError(f"log {needle!r} not observed within {timeout}s; logs={logs}")


def test_idle_poll_seconds_bounds(monkeypatch):
    monkeypatch.delenv("DSH_SIM_WORKER_IDLE_POLL_SECONDS", raising=False)
    assert idle_poll_seconds() == 2.0
    with pytest.raises(ValueError):
        idle_poll_seconds({"DSH_SIM_WORKER_IDLE_POLL_SECONDS": "999"})


def test_persistent_worker_claims_job_after_empty_queue(tmp_path, monkeypatch):
    """先在空队列启动 worker → 再经 API 创建任务/准备作业 → worker 仍能接单。"""
    monkeypatch.setenv(
        "DSH_SIM_DATABASE_URL", f"sqlite:///{(tmp_path / 'api.db').as_posix()}"
    )
    monkeypatch.setenv("DSH_SIM_WORKER_IDLE_POLL_SECONDS", "0.2")

    from dsh_sim.api.main import create_app
    from dsh_sim.worker.loop import WorkerConfig

    application = create_app(
        database_url=f"sqlite:///{(tmp_path / 'api.db').as_posix()}",
        artifact_root=tmp_path / "artifacts",
        identity_mode="dev",
    )
    with TestClient(application) as client:
        logs: list[str] = []
        stop = StopFlag()
        config = WorkerConfig(
            node_id="node-svc-test",
            work_root=tmp_path / "worker",
            artifact_root=tmp_path / "artifacts",
            poll_interval_seconds=0.0,
        )
        thread = threading.Thread(
            target=serve,
            kwargs=dict(config=config, stop=stop, log=logs.append, install_signals=False),
            daemon=True,
        )
        thread.start()

        # 1) worker 启动并在空队列上存活（不是 run_until_idle 的空即退）。
        _wait_for_log(logs, "worker started")
        _wait_for_log(logs, "queue idle")

        # 2) worker 已在等待时，经 API 创建任务并发起准备作业。
        r = client.post(
            "/api/v1/tasks",
            json={"draft": {"purpose": "design_screening"}},
            headers={**EXECUTOR_HEADERS, **ik()},
        )
        assert r.status_code == 201, r.text
        task_id = r.json()["task_id"]
        r = client.post(
            f"/api/v1/tasks/{task_id}/revisions",
            json={"expected_revision": 0, "spec": make_spec()},
            headers={**EXECUTOR_HEADERS, **ik()},
        )
        assert r.status_code == 201, r.text
        r = client.post(
            f"/api/v1/tasks/{task_id}/prepare",
            json={"revision": 1},
            headers={**EXECUTOR_HEADERS, **ik()},
        )
        assert r.status_code == 202, r.text
        preparation_id = r.json()["preparation_id"]

        # 3) 常驻 worker 领取并完成该作业。
        _wait_for_log(logs, "processed 1 job(s)")

        # 4) 优雅停止：不再领取新作业并正常退出线程。
        stop.request_stop()
        thread.join(timeout=10)
        assert not thread.is_alive(), "worker thread did not stop gracefully"
        _wait_for_log(logs, "stopped gracefully")

        r = client.get(f"/api/v1/preparations/{preparation_id}", headers=EXECUTOR_HEADERS)
        assert r.status_code == 200, r.text
        prep = r.json()
        assert prep["prepared_digest"], prep
        assert prep["blockers"] == [], prep  # mock 链路准备就绪且无差异


# ---------------------------------------------------------------------------
# 断点返修 2：授权读取投影（GET /tasks/{id}/authorizations）
# ---------------------------------------------------------------------------


def _prepared_task(client, tmp_path):
    """单 variant 任务：prepare + 进程内 worker 消化 → 返回 (task_id, prep)。"""
    from conftest import make_spec
    from helpers_chain import run_worker

    spec = make_spec()
    spec["variants"] = [spec["variants"][0]]
    r = client.post("/api/v1/tasks", json={"draft": {"purpose": "design_screening"}},
                    headers={**EXECUTOR_HEADERS, **ik()})
    assert r.status_code == 201, r.text
    task_id = r.json()["task_id"]
    r = client.post(f"/api/v1/tasks/{task_id}/revisions",
                    json={"expected_revision": 0, "spec": spec},
                    headers={**EXECUTOR_HEADERS, **ik()})
    assert r.status_code == 201, r.text
    r = client.post(f"/api/v1/tasks/{task_id}/prepare", json={"revision": 1},
                    headers={**EXECUTOR_HEADERS, **ik()})
    assert r.status_code == 202, r.text
    preparation_id = r.json()["preparation_id"]
    run_worker(client, tmp_path)
    prep = client.get(f"/api/v1/preparations/{preparation_id}",
                      headers=EXECUTOR_HEADERS).json()
    assert prep["blockers"] == [], prep
    return task_id, prep


def _authorize(client, task_id, prep):
    from helpers_chain import make_spec3

    r = client.post("/api/v1/confirmations",
                    json={"action": "authorizeRuns", "target_id": task_id,
                          "target_digest": prep["prepared_digest"]},
                    headers={**EXECUTOR_HEADERS, **ik()})
    assert r.status_code == 201, r.text
    confirmation_id = r.json()["confirmation_id"]
    r = client.post(f"/api/v1/tasks/{task_id}/authorizations",
                    json={"revision": 1, "preparation_id": prep["preparation_id"],
                          "prepared_digest": prep["prepared_digest"],
                          "execution_budget": make_spec3()["execution_budget"],
                          "confirmation_id": confirmation_id},
                    headers={**EXECUTOR_HEADERS, **ik()})
    assert r.status_code == 201, r.text
    return r.json()


def test_authorization_read_projection_scopes_and_fields(client, tmp_path):
    from conftest import AGENT_HEADERS, OTHER_PROJECT_HEADERS, REVIEWER_HEADERS

    task_id, prep = _prepared_task(client, tmp_path)
    auth = _authorize(client, task_id, prep)

    # 同项目执行者可读回；字段含交接所需 ids/digest，不含 confirmation 凭据。
    r = client.get(f"/api/v1/tasks/{task_id}/authorizations", headers=EXECUTOR_HEADERS)
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert len(items) == 1, items
    item = items[0]
    assert item["authorization_id"] == auth["authorization_id"]
    assert item["prepared_digest"] == prep["prepared_digest"]
    assert item["validity"] == "CURRENT" and item["revoked_at"] is None
    assert all("confirmation" not in k.lower() for k in item)

    # 同项目审查者与 AGENT（runner）同样可读（同项目成员；提交仍由服务端校验）。
    assert client.get(f"/api/v1/tasks/{task_id}/authorizations",
                      headers=REVIEWER_HEADERS).status_code == 200
    assert client.get(f"/api/v1/tasks/{task_id}/authorizations",
                      headers=AGENT_HEADERS).status_code == 200

    # 跨项目身份不可读（项目权限保护）。
    r = client.get(f"/api/v1/tasks/{task_id}/authorizations", headers=OTHER_PROJECT_HEADERS)
    assert r.status_code in (403, 404), r.status_code


def test_recovered_authorization_drives_single_agent_submit(client, tmp_path):
    """恢复读回的 authorization_id 交给 AGENT runner 首次提交；授权不重复。"""
    from conftest import AGENT_HEADERS
    from helpers_chain import ik as _ik

    task_id, prep = _prepared_task(client, tmp_path)
    _authorize(client, task_id, prep)

    # 模拟"刷新后恢复"：重新读取投影取授权（面板行为）。
    items = client.get(f"/api/v1/tasks/{task_id}/authorizations",
                       headers=EXECUTOR_HEADERS).json()["items"]
    current = [a for a in items if a["validity"] == "CURRENT" and a["revision"] == 1]
    assert len(current) == 1
    recovered = current[0]

    # AGENT runner 用恢复读回的授权首次提交（幂等键独立）。
    r = client.post(f"/api/v1/tasks/{task_id}/submissions",
                    json={"authorization_id": recovered["authorization_id"],
                          "prepared_digest": recovered["prepared_digest"]},
                    headers={**AGENT_HEADERS, **_ik()})
    assert r.status_code == 202, r.text
    run_ids = r.json()["run_ids"]
    assert len(run_ids) == 1

    # 不重复授权：有效授权仍只有一条；面板侧无需也无法再签发新授权。
    items2 = client.get(f"/api/v1/tasks/{task_id}/authorizations",
                        headers=EXECUTOR_HEADERS).json()["items"]
    assert len([a for a in items2 if a["validity"] == "CURRENT"]) == 1
    assert len(items2) == 1
