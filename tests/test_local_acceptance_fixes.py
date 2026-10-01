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
    assert "projects: 'proj_a'" in src, "面板必须与 MCP/worker 使用同一专用测试项目"


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


def test_reviewer_panel_uses_dedicated_project():
    src = _read("panels/reviewer/app.js")
    assert "projects: 'proj_a'" in src


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
