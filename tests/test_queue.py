"""队列测试：claim 原子性、租约过期→LOST、晚到事件不覆盖 CANCELLED、fencing token（mock 层）。"""
from __future__ import annotations

from datetime import timedelta

import pytest

from dsh_sim.db.models import AttemptRow, JobRow, LeaseRow, RunRow, TaskRow, utcnow
from dsh_sim.domain.errors import ApiError
from dsh_sim.queue import service as q

pytestmark = pytest.mark.mock


def _task(session) -> None:
    session.add(
        TaskRow(task_id="task_q1", project_id="proj_a", owner_id="eng_zhang", current_revision=1)
    )
    session.flush()


def _execute_job(session, job_id="job_q1", with_attempt=True) -> JobRow:
    _task(session)
    run = RunRow(
        run_id="run_q1", task_id="task_q1", revision=1, variant_id="A", condition_id="C1"
    )
    session.add(run)
    session.flush()
    attempt_id = None
    if with_attempt:
        att = AttemptRow(attempt_id="att_q1", run_id="run_q1", attempt_no=1)
        session.add(att)
        session.flush()
        attempt_id = att.attempt_id
    job = q.enqueue(session, kind="EXECUTE", task_id="task_q1", run_id="run_q1", attempt_id=attempt_id)
    job.job_id = job_id
    session.flush()
    return job


class TestClaimAtomicity:
    def test_two_claims_single_winner(self, session):
        """两个 claim 只有一个赢：第二个拿到 None（作业已被租出）。"""
        _execute_job(session)
        job1, lease1 = q.claim(session, node_id="node_1")
        assert job1 is not None and lease1 is not None
        job2, lease2 = q.claim(session, node_id="node_2")
        assert job2 is None and lease2 is None

    def test_fencing_token_monotonic(self, session):
        _execute_job(session)
        _, lease1 = q.claim(session, node_id="node_1")
        assert lease1.fencing_token == 1
        # 租约失效后重新领取，token 递增
        lease1.active = False
        job = session.get(JobRow, "job_q1")
        job.state = "QUEUED"
        session.flush()
        _, lease2 = q.claim(session, node_id="node_1")
        assert lease2.fencing_token == 2

    def test_node_quota_respected(self, session):
        """节点并发配额：max_concurrent=1 的节点不能同时持有两个活动作业。"""
        _execute_job(session)
        job2 = q.enqueue(session, kind="EXECUTE", task_id="task_q1")
        session.flush()
        j1, _ = q.claim(session, node_id="node_1")
        assert j1 is not None
        j2, _ = q.claim(session, node_id="node_1")
        assert j2 is None  # 配额占满


class TestLeaseExpiry:
    def test_expired_lease_turns_lost(self, session):
        _execute_job(session)
        _, lease = q.claim(session, node_id="node_1")
        future = utcnow() + timedelta(seconds=q.LEASE_SECONDS + 1)
        lost = q.expire_lost_leases(session, now=future)
        assert lost == ["job_q1"]
        job = session.get(JobRow, "job_q1")
        assert job.state == "LOST"
        # LOST 冻结重派：作业不再可领取
        job2, _ = q.claim(session, node_id="node_2")
        assert job2 is None

    def test_fresh_lease_survives(self, session):
        _execute_job(session)
        _, lease = q.claim(session, node_id="node_1")
        lost = q.expire_lost_leases(session, now=utcnow() + timedelta(seconds=10))
        assert lost == []
        assert session.get(JobRow, "job_q1").state == "LEASED"


class TestEventProtocol:
    def _claimed(self, session):
        _execute_job(session)
        job, lease = q.claim(session, node_id="node_1")
        return job, lease

    def test_stale_fencing_token_rejected(self, session):
        job, lease = self._claimed(session)
        with pytest.raises(ApiError, match="fencing"):
            q.post_event(
                session,
                job_id=job.job_id,
                lease_id=lease.lease_id,
                fencing_token=lease.fencing_token + 99,
                event_seq=1,
                kind="STARTING",
            )

    def test_event_seq_must_be_strictly_increasing(self, session):
        job, lease = self._claimed(session)
        with pytest.raises(ApiError, match="空洞"):
            q.post_event(
                session,
                job_id=job.job_id,
                lease_id=lease.lease_id,
                fencing_token=lease.fencing_token,
                event_seq=5,
                kind="STARTING",
            )

    def test_duplicate_event_idempotent(self, session):
        job, lease = self._claimed(session)
        e1 = q.post_event(
            session,
            job_id=job.job_id,
            lease_id=lease.lease_id,
            fencing_token=lease.fencing_token,
            event_seq=1,
            kind="STARTING",
        )
        e2 = q.post_event(
            session,
            job_id=job.job_id,
            lease_id=lease.lease_id,
            fencing_token=lease.fencing_token,
            event_seq=1,
            kind="STARTING",
        )
        assert e1.event_id == e2.event_id

    def test_state_propagates_to_attempt_and_run(self, session):
        job, lease = self._claimed(session)
        q.post_event(
            session,
            job_id=job.job_id,
            lease_id=lease.lease_id,
            fencing_token=lease.fencing_token,
            event_seq=1,
            kind="STARTING",
        )
        q.post_event(
            session,
            job_id=job.job_id,
            lease_id=lease.lease_id,
            fencing_token=lease.fencing_token,
            event_seq=2,
            kind="RUNNING",
        )
        assert session.get(JobRow, "job_q1").state == "RUNNING"
        assert session.get(AttemptRow, "att_q1").state == "RUNNING"
        assert session.get(RunRow, "run_q1").execution_state == "RUNNING"


class TestCancel:
    def test_cancel_queued_is_immediate(self, session):
        """未出租作业无外部进程：现场确认成立 → 直接 CANCELLED。"""
        job = _execute_job(session)
        q.request_cancel(session, job_id=job.job_id)
        assert job.state == "CANCELLED"
        assert session.get(RunRow, "run_q1").execution_state == "CANCELLED"

    def test_cancel_leased_is_async(self, session):
        """已出租作业：请求入库转 CANCELLING，现场确认后才 CANCELLED。"""
        _execute_job(session)
        job, lease = q.claim(session, node_id="node_1")
        q.request_cancel(session, job_id=job.job_id)
        assert job.state == "CANCELLING"
        q.post_event(
            session,
            job_id=job.job_id,
            lease_id=lease.lease_id,
            fencing_token=lease.fencing_token,
            event_seq=1,
            kind="CANCELLED",
            payload={"reason": "process group exited"},
        )
        assert session.get(JobRow, "job_q1").state == "CANCELLED"

    def test_late_completion_does_not_override_cancelled(self, session):
        """晚到完成不得覆盖已撤销状态（定义书 §数据库约束）。"""
        _execute_job(session)
        job, lease = q.claim(session, node_id="node_1")
        q.request_cancel(session, job_id=job.job_id)
        q.post_event(
            session,
            job_id=job.job_id,
            lease_id=lease.lease_id,
            fencing_token=lease.fencing_token,
            event_seq=1,
            kind="CANCELLED",
        )
        assert job.state == "CANCELLED"
        # Worker 迟到的 COMPLETED：事件入审计日志，但状态保持 CANCELLED
        q.post_event(
            session,
            job_id=job.job_id,
            lease_id=lease.lease_id,
            fencing_token=lease.fencing_token,
            event_seq=2,
            kind="COMPLETED",
            payload={"exit_code": 0},
        )
        assert session.get(JobRow, "job_q1").state == "CANCELLED"
        assert session.get(RunRow, "run_q1").execution_state == "CANCELLED"
        assert session.get(AttemptRow, "att_q1").state == "CANCELLED"


class TestHeartbeat:
    def test_heartbeat_refreshes_lease(self, session):
        _execute_job(session)
        job, lease = q.claim(session, node_id="node_1")
        before = lease.last_heartbeat_at
        q.heartbeat(
            session,
            job_id=job.job_id,
            lease_id=lease.lease_id,
            fencing_token=lease.fencing_token,
        )
        assert lease.last_heartbeat_at >= before

    def test_heartbeat_bad_token_rejected(self, session):
        _execute_job(session)
        job, lease = q.claim(session, node_id="node_1")
        with pytest.raises(ApiError):
            q.heartbeat(
                session, job_id=job.job_id, lease_id=lease.lease_id, fencing_token=999
            )
