"""上游验收报告问题 3（2026-09-22）：WAL 恢复必须先确认数据库提交，再确认本地 ACK。

故障注入覆盖三个窗口（报告要求：不要只测正常恢复）：
- commit 前失败   → 记录保留可重放，数据库无事件；
- commit 后/ACK 前崩溃 → 数据库有事件、本地未 ACK，下次重放幂等去重；
- ACK 之后        → 重放为 no-op。

另有顺序证明（ACK 时刻数据库必须已可见该事件）与保留语义（旧租约/序号空洞
保留待人工核查，不静默丢弃、不误标 ACK）。

断言用**独立只读连接**读已提交状态（raw sqlite3，逐条关闭）：本项目的
db/session.py 把所有事务提升为 BEGIN IMMEDIATE，任何经 ORM 的读取都要先取写锁，
会被重放会话尚未收尾的事务挡住；原生只读查询只需要 SHARED 锁，因此能给出
不依赖被测会话的、真正独立的已提交视图。
"""
from __future__ import annotations

import sqlite3

import pytest
from conftest import EXECUTOR_HEADERS

from dsh_sim.queue import service as queue_service
from dsh_sim.worker.loop import _EventChannel, replay_wal
from dsh_sim.worker.wal import JsonlWal

from helpers_chain import (
    authorize_and_submit,
    create_task_with_revision,
    ik,
    prepare_via_worker,
)

pytestmark = pytest.mark.mock


def _wal(tmp_path) -> JsonlWal:
    return JsonlWal(tmp_path / "worker" / "worker.wal.jsonl")


def _job_with_active_lease(client, tmp_path):
    """任务 → 准备（Worker 消费 PREPARE）→ 授权提交（产生 QUEUED 的 EXECUTE 作业）
    → claim 得到一个活动租约（WAL 重放的合法前提）。"""
    task_id = create_task_with_revision(client)
    prep = prepare_via_worker(client, tmp_path, task_id)
    authorize_and_submit(client, task_id, prep)
    s = client.app.state.session_factory()
    job, lease = queue_service.claim(s, node_id="node-wal")
    s.commit()
    assert job is not None and lease is not None, "夹具需要至少一个可领取作业"
    assert lease.active is True
    return s, job, lease


def _append(wal: JsonlWal, job, lease, *, event_seq: int, kind: str = "STARTING") -> None:
    wal.append(
        job_id=job.job_id,
        lease_id=lease.lease_id,
        fencing_token=lease.fencing_token,
        event_seq=event_seq,
        kind=kind,
        payload={"stage": "wal-test", "event_seq": event_seq},
    )


def _raw_scalar(client, sql: str, params: tuple = ()) -> int | str:
    """独立只读连接读已提交状态（逐条关闭，不残留连接/游标）。"""
    db_path = client.app.state.engine.url.database
    con = sqlite3.connect(db_path, isolation_level=None, timeout=5)
    try:
        cur = con.execute(sql, params)
        try:
            return cur.fetchone()[0]
        finally:
            cur.close()
    finally:
        con.close()


def _event_count(client, job_id: str, event_seq: int | None = None) -> int:
    if event_seq is None:
        return int(_raw_scalar(client, "SELECT COUNT(*) FROM events WHERE job_id = ?", (job_id,)))
    return int(
        _raw_scalar(
            client,
            "SELECT COUNT(*) FROM events WHERE job_id = ? AND event_seq = ?",
            (job_id, event_seq),
        )
    )


def _job_state(client, job_id: str) -> str:
    return str(_raw_scalar(client, "SELECT state FROM jobs WHERE job_id = ?", (job_id,)))


# ---------------------------------------------------------------------------
# 顺序：先 DB commit，后本地 ACK
# ---------------------------------------------------------------------------
class TestCommitBeforeAck:
    def test_replay_acks_only_after_db_commit_is_visible(
        self, client, tmp_path, monkeypatch
    ):
        """ACK 时刻用**独立会话**核对：事件必须已提交可见（证明 commit 先于 ACK）。"""
        s, job, lease = _job_with_active_lease(client, tmp_path)
        try:
            wal = _wal(tmp_path)
            _append(wal, job, lease, event_seq=1)
            real_mark = wal.mark_acked
            seen: dict[str, int] = {}

            def spy(job_id: str, event_seq: int) -> None:
                seen["visible_at_ack"] = _event_count(client, job_id, event_seq)
                real_mark(job_id, event_seq)

            monkeypatch.setattr(wal, "mark_acked", spy)
            assert replay_wal(s, wal) == 1
            assert seen["visible_at_ack"] == 1, "ACK 之前事件必须已经提交到数据库"
            assert wal.unacked() == []
        finally:
            s.close()

    def test_normal_post_path_also_commits_before_ack(self, client, tmp_path, monkeypatch):
        """正常 post 路径同样先 commit 再 ACK（回归守卫）。"""
        s, job, lease = _job_with_active_lease(client, tmp_path)
        try:
            wal = _wal(tmp_path)
            seen: dict[str, int] = {}
            real_mark = wal.mark_acked

            def spy(job_id: str, event_seq: int) -> None:
                seen["visible_at_ack"] = _event_count(client, job_id, event_seq)
                real_mark(job_id, event_seq)

            monkeypatch.setattr(wal, "mark_acked", spy)
            channel = _EventChannel(s, wal, job, lease)
            channel.post("STARTING", {"stage": "normal-post"})
            assert seen["visible_at_ack"] == 1
            assert wal.unacked() == []
        finally:
            s.close()


# ---------------------------------------------------------------------------
# 窗口 1：commit 前失败 → 记录保留可重放
# ---------------------------------------------------------------------------
class TestFailureBeforeCommit:
    def test_commit_failure_keeps_record_replayable(self, client, tmp_path, monkeypatch):
        s, job, lease = _job_with_active_lease(client, tmp_path)
        try:
            wal = _wal(tmp_path)
            _append(wal, job, lease, event_seq=1)

            calls = {"n": 0}
            real_commit = s.commit

            def flaky_commit():
                calls["n"] += 1
                if calls["n"] == 1:
                    raise RuntimeError("injected: commit 失败（窗口 1）")
                return real_commit()

            monkeypatch.setattr(s, "commit", flaky_commit)
            with pytest.raises(RuntimeError):
                replay_wal(s, wal)

            # 记录未 ACK、原样留在 WAL；数据库没有该事件
            assert len(wal.unacked()) == 1
            assert _event_count(client, job.job_id) == 0

            # 探针：提交失败后数据库不应被锁死——应用仍能写入
            probe = client.post(
                "/api/v1/tasks",
                json={"draft": {"purpose": "design_screening"}},
                headers={**EXECUTOR_HEADERS, **ik()},
            )
            assert probe.status_code == 201, f"提交失败后数据库被锁死: {probe.text[:200]}"

            # 故障消失后重放 → 恰好一条事件并完成 ACK
            assert replay_wal(s, wal) == 1
            assert wal.unacked() == []
            assert _event_count(client, job.job_id, 1) == 1
        finally:
            s.close()


# ---------------------------------------------------------------------------
# 窗口 2：commit 后 / ACK 前崩溃 → 幂等重放，不重复、不非法迁移
# ---------------------------------------------------------------------------
class TestCrashBetweenCommitAndAck:
    def test_crash_before_ack_replays_idempotently(self, client, tmp_path, monkeypatch):
        s, job, lease = _job_with_active_lease(client, tmp_path)
        try:
            wal = _wal(tmp_path)
            _append(wal, job, lease, event_seq=1)

            def crash_on_ack(job_id: str, event_seq: int) -> None:
                raise RuntimeError("injected: commit 后、ACK 前崩溃（窗口 2）")

            monkeypatch.setattr(wal, "mark_acked", crash_on_ack)
            with pytest.raises(RuntimeError):
                replay_wal(s, wal)

            # 提交已落定：数据库有事件；本地未 ACK（因此会被重放）
            assert _event_count(client, job.job_id, 1) == 1
            assert len(wal.unacked()) == 1

            monkeypatch.undo()  # 恢复 ACK 能力，模拟重启后再次恢复
            assert replay_wal(s, wal) == 1
            assert wal.unacked() == []

            # 幂等：仍然只有一条事件，且状态未被非法二次迁移
            assert _event_count(client, job.job_id, 1) == 1
            assert _job_state(client, job.job_id) == "STARTING"
        finally:
            s.close()


# ---------------------------------------------------------------------------
# 窗口 3：ACK 之后 → 重放为 no-op
# ---------------------------------------------------------------------------
class TestAfterAck:
    def test_replay_after_ack_is_noop(self, client, tmp_path):
        s, job, lease = _job_with_active_lease(client, tmp_path)
        try:
            wal = _wal(tmp_path)
            assert replay_wal(s, wal) == 0  # 空 WAL
            _append(wal, job, lease, event_seq=1)
            assert replay_wal(s, wal) == 1
            assert replay_wal(s, wal) == 0  # 已 ACK：无未确认记录
            assert _event_count(client, job.job_id, 1) == 1
        finally:
            s.close()


# ---------------------------------------------------------------------------
# 保留语义：旧租约 / 序号空洞 → 保留待核查，不误标 ACK
# ---------------------------------------------------------------------------
class TestRetention:
    def test_stale_lease_record_kept_for_manual_review(self, client, tmp_path):
        s, job, lease = _job_with_active_lease(client, tmp_path)
        try:
            wal = _wal(tmp_path)
            wal.append(
                job_id=job.job_id,
                lease_id="lease_stale_fenced",
                fencing_token=lease.fencing_token - 1,
                event_seq=1,
                kind="STARTING",
                payload={"stage": "stale"},
            )
            assert replay_wal(s, wal) == 0
            assert len(wal.unacked()) == 1  # 永不接受，但保留供人工核查
            assert _event_count(client, job.job_id) == 0
        finally:
            s.close()

    def test_sequence_hole_record_kept_not_acked(self, client, tmp_path):
        s, job, lease = _job_with_active_lease(client, tmp_path)
        try:
            wal = _wal(tmp_path)
            _append(wal, job, lease, event_seq=2)  # 序号空洞（缺 seq=1）
            assert replay_wal(s, wal) == 0
            assert len(wal.unacked()) == 1  # 不静默丢弃、不误标 ACK
            assert _event_count(client, job.job_id) == 0
        finally:
            s.close()
