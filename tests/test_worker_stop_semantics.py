"""断点 C 返修回归：常驻 worker 停止语义（真实队列 + 真实信号 + 真实子进程）。

两项 EXECUTE 作业排队；第一项执行中向真实 worker 子进程分别发送 SIGTERM /
SIGINT：当前作业必须完整完成并落证据后优雅退出（退出码 0），第二项保持
未领取（无租约、无事件），之后仍可被新 worker 正常领取。

求解器为明确标记的 MOCK：MockStarAdapter(behavior="staged") 内存状态机 +
`run.mock.log`（# MOCK 标注），不涉任何真实求解器。用
DSH_SIM_WORKER_POLL_INTERVAL_SECONDS 把 staged 推进放慢到秒级，为发信号
留出确定窗口。
"""
from __future__ import annotations

import os
import signal
import sqlite3
import subprocess
import sys
import time

import pytest
from fastapi.testclient import TestClient

from conftest import EXECUTOR_HEADERS, make_spec
from helpers_chain import authorize_and_submit, ik, run_worker

from dsh_sim.api.main import create_app

pytestmark = [pytest.mark.mock, pytest.mark.integration]


def _wait_for_event(db: str, job_id: str, kind: str, timeout: float = 25.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        conn = sqlite3.connect(db)
        try:
            row = conn.execute(
                "select 1 from events where job_id=? and kind=? limit 1", (job_id, kind)
            ).fetchone()
        finally:
            conn.close()
        if row:
            return
        time.sleep(0.2)
    raise AssertionError(f"event {kind} for {job_id} not observed within {timeout}s")


def _job_state(db: str, job_id: str) -> tuple[str, int, int]:
    conn = sqlite3.connect(db)
    try:
        state = conn.execute("select state from jobs where job_id=?", (job_id,)).fetchone()
        lease = conn.execute(
            "select count(*) from leases where job_id=? and active=1", (job_id,)
        ).fetchone()[0]
        events = conn.execute("select count(*) from events where job_id=?", (job_id,)).fetchone()[0]
    finally:
        conn.close()
    return (state[0] if state else "MISSING"), lease, events


def _queue_two_executes(tmp_path) -> tuple[TestClient, str, str, str]:
    """建两个任务，PREPARE 由进程内 worker 快速消化，随后提交两个 EXECUTE 入队。"""
    spec = make_spec()
    spec["variants"] = [spec["variants"][0]]  # 单 variant：每任务恰好 1 个 EXECUTE 作业
    db = f"sqlite:///{(tmp_path / 'api.db').as_posix()}"
    app = create_app(
        database_url=db,
        artifact_root=tmp_path / "artifacts",
        identity_mode="dev",  # 身份模式 fail-closed：测试显式声明本地开发模式
    )
    client = TestClient(app)
    ctx = client.__enter__()
    preps: list[tuple[str, dict]] = []
    # 先完成两个任务的 PREPARE（队列里只有 PREPARE 作业），再统一提交 EXECUTE，
    # 保证信号测试开始时队列里恰好是两个 EXECUTE 作业。
    for n in range(2):
        r = ctx.post(
            "/api/v1/tasks",
            json={"draft": {"purpose": "design_screening"}},
            headers={**EXECUTOR_HEADERS, **ik()},
        )
        assert r.status_code == 201, r.text
        task_id = r.json()["task_id"]
        r = ctx.post(
            f"/api/v1/tasks/{task_id}/revisions",
            json={"expected_revision": 0, "spec": spec},
            headers={**EXECUTOR_HEADERS, **ik()},
        )
        assert r.status_code == 201, r.text
        r = ctx.post(
            f"/api/v1/tasks/{task_id}/prepare",
            json={"revision": 1},
            headers={**EXECUTOR_HEADERS, **ik()},
        )
        assert r.status_code == 202, r.text
        # PREPARE 由进程内 staged MOCK 立即消化（poll_interval=0）；此时队列无 EXECUTE。
        run_worker(ctx, tmp_path, node_id=f"prep-drain-{n}")
        prep = ctx.get(
            f"/api/v1/preparations/{r.json()['preparation_id']}", headers=EXECUTOR_HEADERS
        ).json()
        assert prep["blockers"] == [], prep
        preps.append((task_id, prep))
    job_ids: list[str] = []
    for task_id, prep in preps:
        run_ids = authorize_and_submit(ctx, task_id, prep)
        assert len(run_ids) == 1
        conn = sqlite3.connect((tmp_path / "api.db").as_posix())
        try:
            row = conn.execute(
                "select job_id from jobs where run_id=? and kind='EXECUTE'", (run_ids[0],)
            ).fetchone()
        finally:
            conn.close()
        job_ids.append(row[0])
    return ctx, db, job_ids[0], job_ids[1]


def _signal_during_first_job(tmp_path, signum: int) -> None:
    ctx, db, first_job, second_job = _queue_two_executes(tmp_path)
    db_file = (tmp_path / "api.db").as_posix()

    env = os.environ.copy()
    env.update(
        {
            "DSH_SIM_DATABASE_URL": f"sqlite:///{db_file}",
            "DSH_SIM_ARTIFACT_ROOT": str(tmp_path / "artifacts"),
            "DSH_SIM_WORKER_WORK_DIR": str(tmp_path / "worker"),
            "DSH_SIM_WORKER_ADAPTER": "mock",
            "DSH_SIM_WORKER_MOCK_BEHAVIOR": "staged",
            "DSH_SIM_WORKER_POLL_INTERVAL_SECONDS": "2.0",  # staged 慢放：RUNNING 窗口 ≥6s
            "DSH_SIM_WORKER_IDLE_POLL_SECONDS": "0.2",
            "DSH_SIM_WORKER_NODE_ID": f"stop-semantics-{signum}",
        }
    )
    proc = subprocess.Popen(
        [sys.executable, "-m", "dsh_sim.worker.service"],
        env=env,
        cwd=str(tmp_path),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=(os.name != "nt"),
    )
    try:
        # 第一项已被领取并在执行（RUNNING 事件出现后再缓冲 1s，staged 还有 ≥2 步 × 2s）。
        _wait_for_event(db_file, first_job, "RUNNING")
        time.sleep(1.0)
        os.kill(proc.pid, signum)
        # 当前作业完整完成 + 领取边界停止 → 优雅退出（退出码 0）。
        rc = proc.wait(timeout=40)
        assert rc == 0, f"worker did not exit gracefully under {signal.Signals(signum).name}: rc={rc}"

        state, lease, events = _job_state(db_file, second_job)
        assert state == "QUEUED", f"second job was claimed: state={state}"
        assert lease == 0, "second job must have no active lease"
        assert events == 0, "second job must have no events (never claimed)"

        conn = sqlite3.connect(db_file)
        try:
            done = conn.execute(
                "select count(*) from events where job_id=? and kind='COMPLETED'", (first_job,)
            ).fetchone()[0]
        finally:
            conn.close()
        assert done == 1, "first job must have completed before exit"
    finally:
        if proc.poll() is None:
            proc.kill()
        ctx.__exit__(None, None, None)

    # 停止后的剩余作业仍可被新 worker 正常领取完成（不是丢失）。
    from dsh_sim.adapters.mock_adapter import MockStarAdapter
    from dsh_sim.worker.loop import WorkerConfig, run_until_idle
    from dsh_sim.db.session import make_engine, make_session_factory
    from dsh_sim.queue.service import find_active_job_for_run  # noqa: F401 (导入自检)

    engine = make_engine(f"sqlite:///{db_file}")
    factory = make_session_factory(engine)
    with factory() as session:
        processed = run_until_idle(
            session,
            MockStarAdapter(behavior="staged"),
            WorkerConfig(
                node_id="after-stop",
                work_root=tmp_path / "worker-after",
                artifact_root=tmp_path / "artifacts",
                poll_interval_seconds=0.0,
            ),
        )
    assert second_job in processed, f"second job not claimable after stop: {processed}"
    state, _, events = _job_state(db_file, second_job)
    assert state == "SUCCEEDED" and events > 0


def test_sigterm_completes_current_job_and_leaves_next_unclaimed(tmp_path):
    _signal_during_first_job(tmp_path, signal.SIGTERM)


def test_sigint_completes_current_job_and_leaves_next_unclaimed(tmp_path):
    _signal_during_first_job(tmp_path, signal.SIGINT)


def test_run_until_idle_should_stop_leaves_queue_untouched(tmp_path):
    """领取边界停止的进程内直接验证：不启动任何作业。"""
    ctx, db, first_job, second_job = _queue_two_executes(tmp_path)
    from dsh_sim.adapters.mock_adapter import MockStarAdapter
    from dsh_sim.db.session import make_engine, make_session_factory
    from dsh_sim.worker.loop import WorkerConfig, run_until_idle

    engine = make_engine(db)
    factory = make_session_factory(engine)
    with factory() as session:
        processed = run_until_idle(
            session,
            MockStarAdapter(behavior="staged"),
            WorkerConfig(
                node_id="boundary",
                work_root=tmp_path / "worker-boundary",
                artifact_root=tmp_path / "artifacts",
            ),
            should_stop=lambda: True,  # 首次领取边界即停止
        )
    assert processed == []
    db_file = db.removeprefix("sqlite:///")
    for job in (first_job, second_job):
        state, lease, events = _job_state(db_file, job)
        assert state == "QUEUED" and lease == 0 and events == 0
    ctx.__exit__(None, None, None)
