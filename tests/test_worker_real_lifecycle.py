"""WP-07/FR-11/12: worker supervision with actual, owned Python child processes.

The process emits explicitly MOCK fixture logs, not solver results. These tests
verify cancellation, transaction release and evidence binding; they do not claim
any STAR-CCM+/OpenFOAM numerical validation. REAL-tagged probes occur only in
negative tests that must stop before launching a process.
"""
from __future__ import annotations

import hashlib
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
from conftest import EXECUTOR_HEADERS, make_spec
from helpers_chain import authorize_and_submit, create_task_with_revision, ik, prepare_via_worker

from dsh_sim.adapters.mock_adapter import MockStarAdapter
from dsh_sim.adapters.star_adapter import CollectedOutputs, JobStatus, RawMetrics
from dsh_sim.canonical import canonical_dumps, sha256_hex
from dsh_sim.db.models import ArtifactRow, AttemptRow, AuthorizationRow, EventRow, JobRow, LeaseRow, RunRow, utcnow
from dsh_sim.queue import service as queue
from dsh_sim.worker.loop import WorkerConfig, run_until_idle

pytestmark = [pytest.mark.mock, pytest.mark.integration]


class ChildProcessAdapter(MockStarAdapter):
    """Real OS process control, deliberately MOCK engineering evidence."""

    def __init__(self, *, poll_error=False, unconfirmed_cancel=False, collect_error=False):
        super().__init__()
        self.poll_error = poll_error
        self.unconfirmed_cancel = unconfirmed_cancel
        self.collect_error = collect_error
        self.started = threading.Event()
        self.processes = {}
        self.cancel_calls = 0
        self.launch_calls = 0

    def launch(self, prepared_ref, budget):
        self.launch_calls += 1
        handle = super().launch(prepared_ref, budget)
        log = Path(handle.work_dir) / "run.mock.log"
        log.write_text("# MOCK lifecycle test process; not solver output\n", encoding="utf-8")
        with log.open("ab") as output:
            proc = subprocess.Popen(
                [sys.executable, "-u", "-c", "import time; print('test child started', flush=True); time.sleep(30)"],
                stdout=output, stderr=subprocess.STDOUT, start_new_session=os.name != "nt",
            )
        self.processes[handle.job_id] = proc
        self.started.set()
        return handle

    def poll(self, handle):
        if self.poll_error == "keyboard":
            raise KeyboardInterrupt("lifecycle test interrupt")
        if self.poll_error == "system_exit":
            raise SystemExit(2)
        if self.poll_error:
            raise RuntimeError("deliberate poll transport failure")
        proc = self.processes[handle.job_id]
        code = proc.poll()
        return JobStatus(handle.job_id, "RUNNING" if code is None else "FAILED", None,
                         str(Path(handle.work_dir) / "run.mock.log"),
                         {"evidence_mode": "MOCK", "exit_code": code})

    def cancel(self, handle):
        self.cancel_calls += 1
        proc = self.processes[handle.job_id]
        if self.unconfirmed_cancel:
            phase = self.unconfirmed_cancel if isinstance(self.unconfirmed_cancel, str) else "LOST"
            return JobStatus(handle.job_id, phase, None, None, {
                "evidence_mode": "MOCK", "exit_proof": {"process_exited": False, "process_group_exited": False},
            })
        self._stop(proc)
        return JobStatus(handle.job_id, "CANCELLED", None, None, {
            "evidence_mode": "MOCK", "exit_code": proc.returncode,
            "exit_proof": {"process_exited": proc.poll() is not None, "process_group_exited": True,
                           "process_identity": handle.process_identity, "returncode": proc.returncode},
        })

    @staticmethod
    def _stop(proc):
        if proc.poll() is None:
            if os.name != "nt":
                os.killpg(proc.pid, signal.SIGTERM)
            else:
                proc.terminate()
        proc.wait(timeout=3)

    def cleanup(self):
        for proc in self.processes.values():
            self._stop(proc)

    def collect_outputs(self, handle):
        if self.collect_error:
            raise RuntimeError("deliberate collector failure")
        log = Path(handle.work_dir) / "run.mock.log"
        return CollectedOutputs(
            run_id=handle.run_id, attempt_no=handle.attempt_no, raw_reports=(str(log),),
            monitors_csv=(), scenes=(), result_sim=None,
            artifact_digests={"raw/run.mock.log": hashlib.sha256(log.read_bytes()).hexdigest()},
            summary={"partial": True, "evidence_mode": "MOCK"}, evidence_mode="MOCK",
        )

    def extract_metrics(self, collected):
        return RawMetrics({}, ("test_process_is_not_a_solver",), "mock-process-fixture", "0.1.0", {}, "MOCK")


class RealProbeOnlyAdapter(MockStarAdapter):
    """Synthetic REAL-tagged probe for fail-before-launch contract tests only."""

    def __init__(self):
        super().__init__()
        self.launch_calls = 0

    def probe_environment(self):
        return replace(super().probe_environment(), evidence_mode="REAL")

    def launch(self, prepared_ref, budget):
        self.launch_calls += 1
        raise AssertionError("negative REAL gate test must not launch")


def _one_spec():
    spec = make_spec()
    spec["variants"] = spec["variants"][:1]
    return spec


def _queued(client, tmp_path, *, seconds=None, spec=None):
    task_id = create_task_with_revision(client, spec or _one_spec())
    preparation = prepare_via_worker(client, tmp_path, task_id)
    run_id = authorize_and_submit(client, task_id, preparation)[0]
    if seconds is not None:
        # Isolated fixture authorization; the production worker still reads the
        # human authorization record, never a model-supplied fallback budget.
        with client.app.state.session_factory() as session:
            auth = session.query(AuthorizationRow).filter_by(task_id=task_id).one()
            auth.execution_budget = {**auth.execution_budget, "wallclock_hours": seconds / 3600}
            session.commit()
    return task_id, run_id, preparation


def _work(client, tmp_path, adapter, *, max_polls=None, interval=0.01):
    config = WorkerConfig("node-supervised", tmp_path / "worker", client.app.state.artifact_root,
                          max_jobs=1, max_polls=max_polls, poll_interval_seconds=interval)
    with client.app.state.session_factory() as session:
        return run_until_idle(session, adapter, config)


def _run_events(client, run_id):
    response = client.get(f"/api/v1/runs/{run_id}", headers=EXECUTOR_HEADERS)
    assert response.status_code == 200, response.text
    return response.json()


def _assert_frozen_log(client, event):
    assert event["payload"]["artifact_ids"]
    with client.app.state.session_factory() as session:
        rows = [session.get(ArtifactRow, aid) for aid in event["payload"]["artifact_ids"]]
        logs = [a for a in rows if a.logical_path.endswith("run.mock.log")]
        assert logs
        for art in logs:
            content = Path(art.storage_path).read_bytes()
            assert hashlib.sha256(content).hexdigest() == art.sha256
            assert b"MOCK lifecycle test" in content
            assert art.evidence_mode == "MOCK"
        session.commit()


def test_wall_budget_terminates_owned_process_and_freezes_failure_log(client, tmp_path):
    _, run_id, _ = _queued(client, tmp_path, seconds=0.08)
    adapter = ChildProcessAdapter()
    try:
        _work(client, tmp_path, adapter)
        body = _run_events(client, run_id)
        assert body["execution_state"] == "FAILED"
        done = body["events"][-1]
        assert done["payload"]["exit_code"] == 124
        assert done["payload"]["exit_unconfirmed"] is False
        assert adapter.cancel_calls == 1
        assert all(p.poll() is not None for p in adapter.processes.values())
        _assert_frozen_log(client, done)
    finally:
        adapter.cleanup()


@pytest.mark.parametrize("collect_error", [False, True])
def test_poll_exception_always_cancels_and_retains_raw_log(client, tmp_path, collect_error):
    _, run_id, _ = _queued(client, tmp_path)
    adapter = ChildProcessAdapter(poll_error=True, collect_error=collect_error)
    try:
        _work(client, tmp_path, adapter)
        body = _run_events(client, run_id)
        assert body["execution_state"] == "FAILED"
        assert "deliberate poll transport failure" in body["events"][-1]["payload"]["reason"]
        assert adapter.cancel_calls == 1
        assert all(p.poll() is not None for p in adapter.processes.values())
        _assert_frozen_log(client, body["events"][-1])
    finally:
        adapter.cleanup()


@pytest.mark.parametrize("reported_phase", ["LOST", "CANCELLED"])
def test_unconfirmed_exit_stays_lost_and_blocks_node_reuse(client, tmp_path, reported_phase):
    _, run_id, _ = _queued(client, tmp_path)
    adapter = ChildProcessAdapter(unconfirmed_cancel=reported_phase)
    try:
        _work(client, tmp_path, adapter, max_polls=1)
        body = _run_events(client, run_id)
        assert body["execution_state"] == "LOST"
        assert body["events"][-1]["kind"] == "FAILED"  # Existing wire event enum, truthful LOST projection.
        assert body["events"][-1]["payload"]["exit_unconfirmed"] is True
        with client.app.state.session_factory() as session:
            job = session.query(JobRow).filter_by(run_id=run_id).one()
            assert session.get(AttemptRow, job.attempt_id).state == "LOST"
            queue.enqueue(session, kind="PREPARE")
            claimed, _ = queue.claim(session, node_id="node-supervised")
            assert claimed is None
            lease = session.query(LeaseRow).filter_by(job_id=job.job_id, active=True).one()
            last = session.query(EventRow.event_seq).filter_by(job_id=job.job_id).order_by(EventRow.event_seq.desc()).first()[0]
            queue.post_event(session, job_id=job.job_id, lease_id=lease.lease_id, fencing_token=lease.fencing_token,
                             event_seq=last + 1, kind="FAILED", payload={"reason": "late worker failure"})
            assert job.state == "LOST"  # Only a separate verified/human resolution can release it.
            session.commit()
        _assert_frozen_log(client, body["events"][-1])
    finally:
        adapter.cleanup()


@pytest.mark.parametrize("error,exception,state", [
    ("keyboard", KeyboardInterrupt, "CANCELLED"), ("system_exit", SystemExit, "FAILED"),
])
def test_process_cleanup_and_evidence_before_propagating_interrupt(client, tmp_path, error, exception, state):
    _, run_id, _ = _queued(client, tmp_path)
    adapter = ChildProcessAdapter(poll_error=error)
    try:
        with pytest.raises(exception):
            _work(client, tmp_path, adapter)
        body = _run_events(client, run_id)
        assert body["execution_state"] == state
        assert adapter.cancel_calls == 1
        assert all(p.poll() is not None for p in adapter.processes.values())
        _assert_frozen_log(client, body["events"][-1])
    finally:
        adapter.cleanup()


def test_waiting_worker_releases_sqlite_lock_for_external_cancel(client, tmp_path):
    _, run_id, _ = _queued(client, tmp_path)
    adapter = ChildProcessAdapter()
    errors = []
    def execute():
        try:
            _work(client, tmp_path, adapter, interval=0.05, max_polls=100)
        except BaseException as exc:
            errors.append(exc)
    worker = threading.Thread(target=execute, daemon=True)
    try:
        worker.start()
        assert adapter.started.wait(2)
        started = time.monotonic()
        response = client.post(f"/api/v1/runs/{run_id}/cancel", json={"reason": "lifecycle test"},
                               headers={**EXECUTOR_HEADERS, **ik()})
        elapsed = time.monotonic() - started
        assert response.status_code == 202, response.text
        assert elapsed < 1.0, "cancel blocked behind a worker database transaction"
        worker.join(3)
        assert not worker.is_alive() and not errors, errors
        body = _run_events(client, run_id)
        assert body["execution_state"] == "CANCELLED"
        assert body["events"][-1]["payload"]["exit_proof"]["process_exited"] is True
        assert all(p.poll() is not None for p in adapter.processes.values())
    finally:
        adapter.cleanup()
        worker.join(3)


def test_real_method_digest_mismatch_stops_before_launch(client, tmp_path):
    _, run_id, _ = _queued(client, tmp_path)
    adapter = RealProbeOnlyAdapter()
    _work(client, tmp_path, adapter)
    body = _run_events(client, run_id)
    assert body["execution_state"] == "FAILED"
    assert "REAL 方法包摘要" in body["events"][-1]["payload"]["reason"]
    assert adapter.launch_calls == 0


def test_real_execution_cannot_promote_mock_prepared_artifact(client, tmp_path):
    import json
    spec = _one_spec()
    manifest = Path(__file__).resolve().parents[1] / "capabilities/buffer_chamber/0.1.0/manifest.json"
    spec["method"]["capability_package_sha256"] = sha256_hex(canonical_dumps(json.loads(manifest.read_text())))
    _, run_id, _ = _queued(client, tmp_path, spec=spec)
    adapter = RealProbeOnlyAdapter()
    _work(client, tmp_path, adapter)
    body = _run_events(client, run_id)
    assert body["execution_state"] == "FAILED"
    assert "MOCK/UNKNOWN 准备产物" in body["events"][-1]["payload"]["reason"]
    assert adapter.launch_calls == 0


def test_prepared_artifact_same_path_and_hash_from_other_task_is_rejected(client, tmp_path):
    other_task = create_task_with_revision(client, _one_spec())
    prepare_via_worker(client, tmp_path, other_task)
    _, run_id, prep = _queued(client, tmp_path)
    with client.app.state.session_factory() as session:
        logical = next(iter(prep["prepared_artifacts"]))
        art = session.query(ArtifactRow).filter_by(logical_path=logical).one()
        other_job = session.query(JobRow).filter_by(task_id=other_task, kind="PREPARE").one()
        art.job_id = other_job.job_id  # Same project/name/bytes, different preparation ownership.
        session.commit()
    adapter = ChildProcessAdapter()
    try:
        _work(client, tmp_path, adapter)
        body = _run_events(client, run_id)
        assert body["execution_state"] == "FAILED"
        assert adapter.launch_calls == 0
    finally:
        adapter.cleanup()


def test_prepared_file_tampering_blocks_launch(client, tmp_path):
    _, run_id, prep = _queued(client, tmp_path)
    with client.app.state.session_factory() as session:
        logical = next(iter(prep["prepared_artifacts"]))
        art = session.query(ArtifactRow).filter_by(logical_path=logical).one()
        Path(art.storage_path).write_text("tampered fixture bytes", encoding="utf-8")
        session.commit()
    adapter = ChildProcessAdapter()
    try:
        _work(client, tmp_path, adapter)
        body = _run_events(client, run_id)
        assert body["execution_state"] == "FAILED"
        assert "准备文件长度/摘要损坏" in body["events"][-1]["payload"]["reason"]
        assert adapter.launch_calls == 0
    finally:
        adapter.cleanup()


@pytest.mark.parametrize("event_channel", [False, True])
def test_live_heartbeat_renews_lease_beyond_initial_expiry(session, event_channel):
    job = queue.enqueue(session, kind="PREPARE")
    started = utcnow()
    job, lease = queue.claim(session, node_id="node-heartbeat", now=started)
    heartbeat_at = started + timedelta(seconds=queue.LEASE_SECONDS - 5)
    kwargs = dict(job_id=job.job_id, lease_id=lease.lease_id, fencing_token=lease.fencing_token, now=heartbeat_at)
    if event_channel:
        queue.post_event(session, event_seq=1, kind="HEARTBEAT", payload={"stage": "running"}, **kwargs)
    else:
        queue.heartbeat(session, **kwargs)
    assert queue.expire_lost_leases(session, now=started + timedelta(seconds=queue.LEASE_SECONDS + 1)) == []
    assert lease.active is True and job.state == "LEASED"


# Recheck uses the same selected-attempt and method binding as initial execution.
def _completed_mock_run(client, tmp_path):
    task_id, run_id, prep = _queued(client, tmp_path)
    _work(client, tmp_path, MockStarAdapter())
    assert _run_events(client, run_id)["execution_state"] == "SUCCEEDED"
    return task_id, run_id, prep


def _recheck(client, run_id):
    return client.post("/api/v1/verifications/recheck", json={"run_id": run_id},
                       headers={**EXECUTOR_HEADERS, **ik()})


def test_recheck_ignores_previous_attempt_artifacts(client, tmp_path):
    import uuid
    _, run_id, _ = _completed_mock_run(client, tmp_path)
    with client.app.state.session_factory() as session:
        run = session.get(RunRow, run_id)
        previous_attempt = run.current_attempt_id
        current_attempt = "att_" + uuid.uuid4().hex[:24]
        session.add(AttemptRow(attempt_id=current_attempt, run_id=run_id, attempt_no=2, state="SUCCEEDED"))
        session.flush()
        run.current_attempt_id = current_attempt
        content = ("# MOCK recheck fixture, not solver output\n"
                   "section,boundary_role,sign_convention,mass_flow_kg_s,total_pressure_pa,static_pressure_pa\n"
                   "inlet,inlet,outward_positive,-1,1300,1200\n"
                   "outlet,outlet,outward_positive,1,1200,1100\n").encode()
        path = tmp_path / "attempt-2-report.mock.csv"
        path.write_bytes(content)
        artifact_id = "art_" + uuid.uuid4().hex[:24]
        session.add(ArtifactRow(artifact_id=artifact_id, project_id="proj_a", run_id=run_id,
            attempt_id=current_attempt, logical_path=f"runs/{run_id}/attempt-2/raw/report.mock.csv",
            storage_path=str(path), length=len(content), sha256=hashlib.sha256(content).hexdigest(),
            state="COMMITTED", evidence_mode="MOCK"))
        old_ids = {a.artifact_id for a in session.query(ArtifactRow).filter_by(run_id=run_id, attempt_id=previous_attempt)}
        session.commit()
    response = _recheck(client, run_id)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["attempt_id"] == current_attempt
    assert body["check_inputs"]["metric_values"]["total_pressure_loss"] == 100.0
    assert set(body["source_artifact_ids"]) == {artifact_id}
    assert not (set(body["source_artifact_ids"]) & old_ids)
    assert "mass_imbalance" in body["check_inputs"]["required_metrics"]


def test_recheck_does_not_substitute_previous_attempt_when_current_has_no_report(client, tmp_path):
    import uuid
    _, run_id, _ = _completed_mock_run(client, tmp_path)
    with client.app.state.session_factory() as session:
        run = session.get(RunRow, run_id)
        current = "att_" + uuid.uuid4().hex[:24]
        session.add(AttemptRow(attempt_id=current, run_id=run_id, attempt_no=2, state="FAILED"))
        session.flush()
        run.current_attempt_id = current
        session.commit()
    response = _recheck(client, run_id)
    assert response.status_code == 503
    assert "无原始报告" in response.json()["message"]


def test_recheck_rejects_changed_raw_bytes(client, tmp_path):
    _, run_id, _ = _completed_mock_run(client, tmp_path)
    with client.app.state.session_factory() as session:
        report = next(a for a in session.query(ArtifactRow).filter_by(run_id=run_id)
                      if Path(a.logical_path).name == "report.mock.csv")
        Path(report.storage_path).write_text("changed after freezing", encoding="utf-8")
        session.commit()
    response = _recheck(client, run_id)
    assert response.status_code == 503
    assert "摘要或长度不符" in response.json()["message"]


def test_real_tagged_recheck_rejects_unmatched_method_digest(client, tmp_path):
    # Gate fixture only: changing the tag does not turn synthetic CSV into a
    # solver result. The negative test must refuse to produce a verification.
    _, run_id, _ = _completed_mock_run(client, tmp_path)
    with client.app.state.session_factory() as session:
        for art in session.query(ArtifactRow).filter_by(run_id=run_id):
            art.evidence_mode = "REAL"
        session.commit()
    response = _recheck(client, run_id)
    assert response.status_code == 503
    assert "REAL 复算方法包摘要" in response.json()["message"]
