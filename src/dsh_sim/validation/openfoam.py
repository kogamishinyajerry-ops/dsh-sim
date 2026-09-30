"""One-command public OpenFOAM validation through the existing simulation core.

Only built-in, public cases are accepted. Every invocation creates a new local
database below a new output directory. Its synthetic authorization row is a
labelled TEST FIXTURE, not a human identity/confirmation or a production grant.
There is no API address, existing DB, arbitrary TaskSpec or approval parameter.
Normal MCP/API authorization gates are untouched.
"""
from __future__ import annotations

import argparse
import html
import json
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy.engine import URL

from dsh_sim.canonical import canonical_dumps, sha256_hex

SCENARIOS = ("baseline", "transfer", "solver-failure", "cancel", "timeout")
FORMAT = "dsh-sim-validation/v1"


def _json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _parameters(scenario: str) -> dict[str, Any]:
    parameters = dict(mean_velocity=0.01, length=1.0, height=0.1, width=0.01, nu=0.001, density=1000.0, nx=80, ny=20, iterations=1000)
    if scenario == "transfer":
        parameters.update(mean_velocity=0.015, length=0.6, height=0.08, width=0.012, nu=0.002, density=950.0, nx=72, ny=24)
    elif scenario == "solver-failure":
        parameters["test_fault"] = "invalid_div_scheme"
    elif scenario in ("cancel", "timeout"):
        parameters["iterations"] = 200000
    return parameters


def _stream_wal(path: Path, stop: threading.Event) -> None:
    """Show the existing worker WAL; do not create a second execution state."""
    seen: set[tuple[str, int]] = set()
    while True:
        if path.is_file():
            for line in path.read_text(encoding="utf-8").splitlines():
                row = json.loads(line)
                key = (row["job_id"], row["event_seq"])
                if key not in seen:
                    print(json.dumps({"source": "worker-wal", **row}, ensure_ascii=False), file=sys.stderr, flush=True)
                    seen.add(key)
        if stop.wait(0.1):
            return


def _cancel_running_fixture(factory, run_id: str, work_root: Path, stop: threading.Event) -> None:
    from dsh_sim.db.models import RunRow
    from dsh_sim.queue.service import find_active_job_for_run, request_cancel

    running_since: float | None = None
    solver_log = work_root / run_id / "attempt-1" / "logs" / "simpleFoam.stdout.log"
    while not stop.wait(0.05):
        solver_started = solver_log.is_file() and "Time = " in solver_log.read_text(encoding="utf-8", errors="replace")
        with factory() as session:
            run = session.get(RunRow, run_id)
            state = run.execution_state
            if state == "RUNNING" and solver_started:
                if running_since is None:
                    running_since = time.monotonic()
                if time.monotonic() - running_since >= 0.2:
                    job = find_active_job_for_run(session, run_id)
                    if job is not None:
                        request_cancel(session, job_id=job.job_id)
                    session.commit()
                    return
            session.commit()  # Release SQLite's explicit BEGIN IMMEDIATE lock.
            if state in {"SUCCEEDED", "FAILED", "CANCELLED", "LOST"}:
                return


def run_validation(destination: str | Path, *, scenario: str = "baseline", stream: bool = True) -> dict[str, Any]:
    """Run a public fixture in a NEW directory and return real observed states."""
    if scenario not in SCENARIOS:
        raise ValueError("Only built-in public validation scenarios are supported")
    destination = Path(destination).absolute()
    if destination.exists() or destination.is_symlink():
        raise ValueError("Output must be a new directory; existing runs are preserved")
    destination.mkdir(parents=True, exist_ok=False)
    base = {"schema_version": FORMAT, "identity_mode": "local_validation_fixture", "scenario": scenario, "started_at": datetime.now(timezone.utc).isoformat()}
    parameters = _parameters(scenario)
    _json(destination / "inputs.json", {**base, "parameters": parameters, "authorization": "Synthetic LOCAL_VALIDATION_FIXTURE row in a new isolated DB; no human approval/acceptance has been issued."})

    # Imports are lazy so --help works without an OpenFOAM installation.
    from dsh_sim.adapters.openfoam_adapter import OpenFoamAdapter, make_channel_template, read_template_metadata, template_sha256
    from dsh_sim.api.services.prep_service import prepare_task
    from dsh_sim.api.services.run_service import submit_runs
    from dsh_sim.api.services.task_service import create_revision, create_task, transition_task_flow
    from dsh_sim.capabilities.registry import register_capabilities
    from dsh_sim.db.models import ArtifactRow, AuthorizationRow, EventRow, JobRow, PreparationRow, RunRow, TaskRow, VerificationRow
    from dsh_sim.db.session import init_db, make_engine, make_session_factory
    from dsh_sim.domain.identity import Identity, Role
    from dsh_sim.domain.states import TaskFlowState
    from dsh_sim.evidence.bundle import build_bundle
    from dsh_sim.evidence.export import export_bundle, verify_export
    from dsh_sim.verify.extract import extract_run_metrics
    from dsh_sim.verify.verifier import load_metric_definitions
    from dsh_sim.worker.loop import WorkerConfig, run_until_idle

    templates = destination / "templates"
    templates.mkdir()
    template = make_channel_template(templates / "channel.tar", **parameters)
    metadata = read_template_metadata(template)
    template_ref = f"public-openfoam-{scenario}"
    adapter = OpenFoamAdapter(template_registry={template_ref: template}, template_root=templates, allowed_work_root=destination / "worker")
    probe = adapter.probe_environment()
    from dataclasses import asdict
    _json(destination / "environment.json", asdict(probe))
    if probe.evidence_mode != "REAL":
        raise ValueError("Public validation requires the real OpenFOAM adapter")
    capabilities = Path(__file__).resolve().parents[3] / "capabilities"
    method_manifest = json.loads((capabilities / "openfoam_channel" / "0.1.0" / "manifest.json").read_text(encoding="utf-8"))
    budget = {"max_concurrent": 1, "cpu_cores": 1, "memory_gb": 1.0, "wallclock_hours": (1.0 if scenario == "timeout" else 60.0) / 3600.0, "max_attempts_total": 1}
    spec = {
        "purpose": "design_screening",
        "variants": [{"variant_id": "public_channel", "template_artifact_id": template_ref, "template_sha256": template_sha256(template), "boundary_map_sha256": metadata["boundary_map_sha256"]}],
        "conditions": [{"condition_id": scenario.replace("-", "_"), "fields": [{"role_id": "inlet", "field": "mean_velocity", "quantity": {"si_value": parameters["mean_velocity"], "unit": "m/s", "physical_meaning": "Area-mean inlet speed for a discretely normalized parabolic profile", "source_ref": f"public-validation:{scenario}:inputs.json"}}]}],
        "execution_budget": budget,
        "method": {"capability_package_id": "openfoam_channel", "capability_package_sha256": sha256_hex(canonical_dumps(method_manifest)), "required_metrics": ["boundary_mass_flow@inlet", "boundary_mass_flow@outlet", "static_pressure@inlet", "static_pressure@outlet"], "review_scope": "PUBLIC LOCAL SOFTWARE VALIDATION ONLY. Synthetic authorization fixture; no human method release or engineering acceptance. Analytical errors are observations, not approval thresholds."},
    }
    _json(destination / "task-spec.json", spec)
    # Explicit path: never consume DSH_SIM_DATABASE_URL or an external API URL.
    engine = make_engine(URL.create("sqlite", database=str(destination / "validation.db")))
    init_db(engine)
    factory = make_session_factory(engine)
    identity = Identity(subject_id="public-validation-agent", roles=frozenset({Role.AGENT}), project_ids=frozenset({"public_validation"}), is_agent=True)
    worker = WorkerConfig(node_id="public-validation-node", work_root=destination / "worker", artifact_root=destination / "artifacts", max_jobs=1, poll_interval_seconds=0.1)
    stop_stream = threading.Event()
    stream_thread = threading.Thread(target=_stream_wal, args=(worker.work_root / "worker.wal.jsonl", stop_stream), daemon=True)
    if stream:
        stream_thread.start()
    try:
        with factory() as session:
            register_capabilities(session, capabilities)
            task = create_task(session, identity, "public_validation", {"purpose": "design_screening"})
            revision = create_revision(session, identity, task.task_id, expected_revision=0, spec=spec, source_refs=[f"public-validation:{scenario}:inputs.json"])
            preparation_id, _ = prepare_task(session, identity, task.task_id, revision=revision.revision)
            session.commit()
            run_until_idle(session, adapter, worker)
            preparation = session.get(PreparationRow, preparation_id)
            session.refresh(preparation)
            if not preparation.ready:
                raise ValueError(f"Preparation blocked: {preparation.blockers}; differences={preparation.differences}")

            # This is deliberately a visible test fixture, not a call to the
            # trusted-human API. Its database is disposable and never exported.
            authorization = AuthorizationRow(authorization_id=f"authz_{uuid.uuid4().hex[:24]}", task_id=task.task_id, revision=revision.revision, preparation_id=preparation_id, prepared_digest=preparation.prepared_digest, execution_budget=budget, authorized_by="LOCAL_VALIDATION_FIXTURE_NOT_HUMAN_APPROVAL", purpose="design_screening", validity="CURRENT")
            session.add(authorization)
            transition_task_flow(session.get(TaskRow, task.task_id), TaskFlowState.AUTHORIZED)
            session.flush()
            run_ids = submit_runs(session, identity, task.task_id, authorization_id=authorization.authorization_id, prepared_digest=preparation.prepared_digest)
            session.commit()
            cancel_stop = threading.Event()
            cancel_thread = None
            if scenario == "cancel":
                cancel_thread = threading.Thread(target=_cancel_running_fixture, args=(factory, run_ids[0], worker.work_root, cancel_stop), daemon=True)
                cancel_thread.start()
            try:
                run_until_idle(session, adapter, worker)
            finally:
                cancel_stop.set()
                if cancel_thread is not None:
                    cancel_thread.join(timeout=5)
            session.expire_all()
            runs = session.query(RunRow).filter(RunRow.run_id.in_(run_ids)).all()
            states = [{"run_id": r.run_id, "execution_state": r.execution_state, "numerical_state": r.numerical_state, "applicability_state": r.applicability_state, "attempt_id": r.current_attempt_id} for r in runs]
            bundle = build_bundle(session, task.task_id, revision=revision.revision, artifact_root=worker.artifact_root)
            session.commit()
            export_bundle(session, bundle, destination / "evidence")
            integrity = verify_export(destination / "evidence")
            observations: dict[str, Any] = {}
            reports = session.query(ArtifactRow).filter_by(run_id=run_ids[0], attempt_id=runs[0].current_attempt_id, state="COMMITTED").all()
            report = next((a for a in reports if a.logical_path.endswith("report.csv")), None)
            if report is not None:
                metrics = extract_run_metrics(report.storage_path, None, metric_definitions=load_metric_definitions("openfoam_channel", "0.1.0")).metric_values
                observed_in = metrics.get("static_pressure@inlet")
                observed_out = metrics.get("static_pressure@outlet")
                reference = 12 * parameters["density"] * parameters["nu"] * parameters["mean_velocity"] * parameters["length"] / parameters["height"] ** 2
                drop = observed_in - observed_out if observed_in is not None and observed_out is not None else None
                observations = {"metric_values": metrics, "static_pressure_drop_pa": drop, "analytical_fully_developed_drop_pa": reference, "relative_reference_difference": abs(drop - reference) / abs(reference) if drop is not None and reference else None, "source_artifact_id": report.artifact_id, "source_sha256": report.sha256, "interpretation": "Diagnostic comparison; no engineering tolerance has been frozen. Source pressure is relative to the solver reference."}
            jobs = session.query(JobRow).filter_by(task_id=task.task_id).all()
            events = session.query(EventRow).filter(EventRow.job_id.in_([j.job_id for j in jobs])).order_by(EventRow.occurred_at, EventRow.event_seq).all()
            (destination / "events.jsonl").write_text("".join(json.dumps({"job_id": e.job_id, "event_seq": e.event_seq, "kind": e.kind, "payload": e.payload, "occurred_at": e.occurred_at.isoformat()}, ensure_ascii=False) + "\n" for e in events), encoding="utf-8")
            verifications = [{"run_id": v.run_id, "conclusion": v.conclusion, "findings": v.findings} for v in session.query(VerificationRow).filter(VerificationRow.run_id.in_(run_ids)).all()]
            expected = {"baseline": "SUCCEEDED", "transfer": "SUCCEEDED", "solver-failure": "FAILED", "cancel": "CANCELLED", "timeout": "FAILED"}[scenario]
            summary = {**base, "evidence_mode": "REAL", "task_id": task.task_id, "revision": revision.revision, "spec_sha256": revision.spec_sha256, "preparation_id": preparation_id, "prepared_digest": preparation.prepared_digest, "run_ids": run_ids, "runs": states, "bundle_id": bundle.bundle_id, "bundle_digest": bundle.bundle_digest, "integrity": integrity, "expected_execution_state": expected, "scenario_assertions_passed": all(r["execution_state"] == expected for r in states), "observations": observations, "verifications": verifications, "report_path": str(destination / "report.html"), "summary_path": str(destination / "summary.json"), "limitations": ["Public local validation fixture; production identity and authorization are not exercised.", "Method is DRAFT; engineering tolerances and applicability remain unconfirmed.", "DSH natural-language/LLM execution is not exercised by this deterministic command.", "Serial OpenFOAM v1912 only; STAR-CCM+, MPI and sim-live-hub are not validated."]}
            _json(destination / "summary.json", summary)
            rows = "".join(f"<tr><td>{html.escape(s['run_id'])}</td><td>{s['execution_state']}</td><td>{s['numerical_state']}</td><td>{s['applicability_state']}</td></tr>" for s in states)
            page = '<!doctype html><html lang="en"><meta charset="utf-8"><title>OpenFOAM validation evidence</title><style>body{font:16px/1.5 system-ui;max-width:1100px;margin:40px auto;padding:0 20px;color:#14283b}table{border-collapse:collapse}td,th{border:1px solid #cbd5df;padding:10px;text-align:left}pre{background:#eff4f8;padding:18px;white-space:pre-wrap;overflow-wrap:anywhere}.notice{padding:16px;background:#fff1c9}</style><body>'
            page += f'<h1>OpenFOAM: {html.escape(scenario)}</h1><p class="notice"><strong>REAL solver evidence · LOCAL VALIDATION FIXTURE</strong><br>No human engineering approval or method release has been issued. Numerical and applicability states are shown independently.</p><table><tr><th>Run</th><th>Execution</th><th>Numerical</th><th>Applicability</th></tr>{rows}</table>'
            page += '<h2>Observed values</h2><pre>' + html.escape(json.dumps(observations, ensure_ascii=False, indent=2)) + '</pre><h2>Evidence</h2><p><a href="evidence/report.offline.html">Frozen report, offline link view</a> · <a href="evidence/export.json">Frozen manifest and file hashes</a> · <a href="events.jsonl">Committed events</a> · <a href="summary.json">Machine-readable summary</a> · <a href="task-spec.json">TaskSpec</a></p><h2>Limits</h2><ul>'
            page += "".join("<li>" + html.escape(item) + "</li>" for item in summary["limitations"]) + '</ul></body></html>'
            (destination / "report.html").write_text(page, encoding="utf-8")
            return summary
    finally:
        stop_stream.set()
        if stream:
            stream_thread.join(timeout=2)
        engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=SCENARIOS, default="baseline")
    parser.add_argument("--output", type=Path, required=True, help="New output directory; existing directories are never overwritten")
    parser.add_argument("--local-validation", action="store_true", help="Acknowledge a new isolated DB with synthetic test-fixture authorization; not a production approval")
    parser.add_argument("--quiet", action="store_true", help="Keep committed events/WAL in files; suppress live stderr event stream")
    args = parser.parse_args(argv)
    if not args.local_validation:
        parser.error("This command is only for explicit --local-validation of built-in public cases")
    try:
        summary = run_validation(args.output, scenario=args.case, stream=not args.quiet)
    except Exception as exc:
        failure = {"schema_version": FORMAT, "scenario": args.case, "status": "BLOCKED", "error_type": type(exc).__name__, "detail": str(exc), "note": "Inspect retained WAL/logs to determine whether execution began. No success is inferred."}
        print(json.dumps(failure, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(summary, ensure_ascii=False, allow_nan=False))
    return 0 if summary["scenario_assertions_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
