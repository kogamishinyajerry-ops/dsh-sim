"""数据库约束与不可变性守卫测试（mock 层）。"""
from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from dsh_sim.db.guards import assert_single_open_attempt
from dsh_sim.db.models import (
    AttemptRow,
    BundleRow,
    DecisionRow,
    EventRow,
    JobRow,
    RunRow,
    TaskRevisionRow,
    TaskRow,
)
from dsh_sim.domain.errors import ApiError

pytestmark = pytest.mark.mock


def _task(session, task_id="task_t1") -> TaskRow:
    row = TaskRow(
        task_id=task_id, project_id="proj_a", owner_id="eng_zhang", current_revision=0
    )
    session.add(row)
    session.flush()
    return row


def _revision(session, task_id="task_t1", revision=1, spec=None) -> TaskRevisionRow:
    row = TaskRevisionRow(
        task_id=task_id,
        revision=revision,
        spec=spec or {"purpose": "design_screening"},
        spec_sha256="a" * 64,
        created_by="eng_zhang",
    )
    session.add(row)
    session.flush()
    return row


def _run(session, run_id="run_1") -> RunRow:
    _task(session)
    row = RunRow(
        run_id=run_id,
        task_id="task_t1",
        revision=1,
        variant_id="A",
        condition_id="C1",
    )
    session.add(row)
    session.flush()
    return row


class TestUniqueConstraints:
    def test_task_revision_unique(self, session):
        _task(session)
        _revision(session, revision=1)
        with pytest.raises(IntegrityError):
            _revision(session, revision=1)

    def test_run_matrix_cell_unique(self, session):
        _run(session)
        dup = RunRow(
            run_id="run_2",
            task_id="task_t1",
            revision=1,
            variant_id="A",
            condition_id="C1",
        )
        session.add(dup)
        with pytest.raises(IntegrityError):
            session.flush()

    def test_attempt_no_unique_per_run(self, session):
        _run(session)
        session.add(AttemptRow(attempt_id="att_1", run_id="run_1", attempt_no=1))
        session.flush()
        session.add(AttemptRow(attempt_id="att_2", run_id="run_1", attempt_no=1))
        with pytest.raises(IntegrityError):
            session.flush()

    def test_event_seq_unique_per_job(self, session):
        session.add(JobRow(job_id="job_1", kind="EXECUTE"))
        session.flush()
        session.add(EventRow(event_id="e1", job_id="job_1", event_seq=1, kind="RUNNING"))
        session.flush()
        session.add(EventRow(event_id="e2", job_id="job_1", event_seq=1, kind="FAILED"))
        with pytest.raises(IntegrityError):
            session.flush()


class TestImmutabilityGuards:
    def test_revision_update_rejected(self, session):
        _task(session)
        rev = _revision(session)
        rev.spec = {"purpose": "tampered"}
        with pytest.raises(ApiError, match="不可原位修改"):
            session.flush()

    def test_bundle_manifest_rewrite_rejected(self, session):
        _task(session)
        bundle = BundleRow(
            bundle_id="bnd_1",
            task_id="task_t1",
            revision=1,
            bundle_digest="c" * 64,
            manifest=[{"artifact_id": "a1", "logical_path": "x.csv", "sha256": "d" * 64}],
        )
        session.add(bundle)
        session.flush()
        bundle.manifest = []
        with pytest.raises(ApiError, match="不可原位改写"):
            session.flush()

    def test_bundle_validity_flip_allowed(self, session):
        """CURRENT→STALE 是失效而非改写，允许。"""
        _task(session)
        bundle = BundleRow(
            bundle_id="bnd_2", task_id="task_t1", revision=1, bundle_digest="c" * 64, manifest=[]
        )
        session.add(bundle)
        session.flush()
        bundle.validity = "STALE"
        session.flush()  # 不抛错

    def test_decision_update_and_delete_rejected(self, session):
        dec = DecisionRow(
            decision_id="dec_1",
            review_id="rev_1",
            outcome="ACCEPT",
            decided_by="rev_li",
            task_id="task_t1",
            revision=1,
            bundle_digest="c" * 64,
        )
        session.add(dec)
        session.commit()  # 先落库，后续篡改/删除各自独立验证
        dec.limitations = "tamper"
        with pytest.raises(ApiError, match="不可变"):
            session.flush()
        session.rollback()
        session.delete(session.get(DecisionRow, "dec_1"))
        with pytest.raises(ApiError, match="不被删除"):
            session.flush()


class TestOpenAttemptGuard:
    def test_open_attempt_blocks_new(self, session):
        _run(session)
        session.add(AttemptRow(attempt_id="att_1", run_id="run_1", attempt_no=1, state="RUNNING"))
        session.flush()
        with pytest.raises(ApiError, match="未核实结束"):
            assert_single_open_attempt(session, "run_1")

    def test_lost_attempt_blocks_retry(self, session):
        """LOST 冻结重派：未核实前不得创建新求解器。"""
        _run(session)
        session.add(AttemptRow(attempt_id="att_1", run_id="run_1", attempt_no=1, state="LOST"))
        session.flush()
        with pytest.raises(ApiError, match="未核实结束"):
            assert_single_open_attempt(session, "run_1")

    def test_terminal_attempt_allows_retry(self, session):
        _run(session)
        session.add(AttemptRow(attempt_id="att_1", run_id="run_1", attempt_no=1, state="FAILED"))
        session.flush()
        assert_single_open_attempt(session, "run_1")  # 不抛错
