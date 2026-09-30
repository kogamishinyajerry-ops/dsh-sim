"""FR-17/FR-18 provenance regressions, using synthetic policy fixtures only.

REAL-labelled inputs in this file exercise JSON/provenance handling. They are
not solver runs, engineering verification, or human approval evidence.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path

import pytest
from conftest import make_spec

from dsh_sim.canonical import canonical_dumps, sha256_hex
from dsh_sim.db.models import (
    ArtifactRow,
    AttemptRow,
    BundleRow,
    ClaimRow,
    EventRow,
    JobRow,
    PreparationRow,
    RunRow,
    TaskRevisionRow,
    TaskRow,
    VerificationRow,
)
from dsh_sim.domain.errors import ApiError
from dsh_sim.evidence.bundle import (
    _draft_claims,
    build_bundle,
    bundle_evidence_mode,
    compute_completeness,
    compute_completeness_manifest,
)

pytestmark = pytest.mark.mock


def _id(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:24]}"


def _task(session, *, method=None):
    spec = make_spec()
    spec["variants"] = spec["variants"][:1]
    if method:
        spec["method"] = method
    task = TaskRow(task_id=_id("task"), project_id="synthetic-project", owner_id="synthetic-owner", current_revision=1)
    session.add(task)
    session.flush()
    session.add(TaskRevisionRow(
        task_id=task.task_id, revision=1, spec=spec,
        spec_sha256=sha256_hex(canonical_dumps(spec)), created_by="synthetic-owner",
    ))
    run = RunRow(
        run_id=_id("run"), task_id=task.task_id, revision=1,
        variant_id="A", condition_id="C1", execution_state="SUCCEEDED",
        numerical_state="INSUFFICIENT", applicability_state="UNCONFIRMED",
    )
    session.add(run)
    session.flush()
    attempts = [
        AttemptRow(attempt_id=_id("attempt"), run_id=run.run_id, attempt_no=n, state="SUCCEEDED")
        for n in (1, 2)
    ]
    session.add_all(attempts)
    run.current_attempt_id = attempts[1].attempt_id
    session.flush()
    return task, run, attempts


def _artifact(session, tmp_path, task, logical, content, *, mode="MOCK", run=None, attempt=None, job_id=None):
    artifact_id = _id("art")
    path = tmp_path / f"{artifact_id}.mock.data"
    path.write_bytes(content)
    art = ArtifactRow(
        artifact_id=artifact_id, project_id=task.project_id, logical_path=logical,
        length=len(content), sha256=hashlib.sha256(content).hexdigest(),
        state="COMMITTED", evidence_mode=mode, storage_path=str(path),
        run_id=run.run_id if run else None,
        attempt_id=attempt.attempt_id if attempt else None, job_id=job_id,
    )
    session.add(art)
    session.flush()
    return art


def _outputs(session, tmp_path, task, run, attempt, *, flow, mode="MOCK"):
    infix = ".mock" if mode == "MOCK" else ""
    base = f"runs/{run.run_id}/attempt-{attempt.attempt_no}"
    report = (
        "section,boundary_role,sign_convention,mass_flow_kg_s,total_pressure_pa,static_pressure_pa\n"
        f"inlet,inlet,outward_positive,{-flow},150.0,100.0\n"
        f"outlet,outlet,outward_positive,{flow},50.0,0.0\n"
    ).encode()
    report_art = _artifact(session, tmp_path, task, f"{base}/raw/report{infix}.csv", report, mode=mode, run=run, attempt=attempt)
    body = json.dumps({
        "evidence_mode": mode,
        "metric_values": {"boundary_mass_flow@inlet": -flow, "boundary_mass_flow@outlet": flow},
        "source_artifacts": {"boundary_mass_flow@inlet": report_art.sha256, "boundary_mass_flow@outlet": report_art.sha256},
    }, indent=2)
    if mode == "MOCK":
        body = "# MOCK DATA - NOT REAL SOLVER OUTPUT\n" + body
    metrics_art = _artifact(session, tmp_path, task, f"{base}/metrics{infix}.json", body.encode(), mode=mode, run=run, attempt=attempt)
    return report_art, metrics_art


def _check(session, run, attempt, report):
    check = VerificationRow(
        verification_id=_id("ver"), run_id=run.run_id, attempt_id=attempt.attempt_id,
        rule_set_sha256="0" * 64, conclusion="INSUFFICIENT",
        check_inputs={"synthetic_policy_fixture": True}, source_artifact_ids=[report.artifact_id],
    )
    session.add(check)
    session.flush()
    return check


def _preparation(session, tmp_path, task, *, mode="MOCK"):
    prep = PreparationRow(
        preparation_id=_id("prep"), task_id=task.task_id, revision=1,
        prepared_digest="b" * 64, readback_sha256="c" * 64,
        adapter_build="synthetic-policy-fixture", ready=True,
    )
    job = JobRow(job_id=_id("job"), kind="PREPARE", task_id=task.task_id, state="SUCCEEDED")
    session.add_all([prep, job])
    session.flush()
    session.add(EventRow(
        event_id=_id("event"), job_id=job.job_id, event_seq=1,
        kind="STARTING", payload={"preparation_id": prep.preparation_id, "revision": 1},
    ))
    artifact = _artifact(
        session, tmp_path, task, f"prepared/{task.task_id}/{prep.preparation_id}/A/C1/prepared.mock.sim",
        b"synthetic preparation, not solver evidence\n", mode=mode, job_id=job.job_id,
    )
    prep.prepared_artifacts = {artifact.logical_path: artifact.sha256}
    session.flush()
    return prep, job, artifact


def test_pretty_real_json_claims_use_only_selected_attempt(session, tmp_path):
    task, run, attempts = _task(session)
    _outputs(session, tmp_path, task, run, attempts[0], flow=1.0, mode="REAL")
    _artifact(session, tmp_path, task, "solver-report.log", b"solver log, not a report CSV", mode="REAL", run=run, attempt=attempts[1])
    _, current_metrics = _outputs(session, tmp_path, task, run, attempts[1], flow=2.0, mode="REAL")

    claims = _draft_claims(session, [run], "synthetic-bundle", metric_defs={"metrics": [{"metric_id": "boundary_mass_flow", "unit": "kg/s"}]})

    inlet = next(claim for claim in claims if claim.metric_id == "boundary_mass_flow@inlet")
    assert inlet.state == "CONFIRMED"
    assert "= -2.0 kg/s" in inlet.text
    assert all(claim.artifact_id == current_metrics.artifact_id for claim in claims)
    assert run.current_attempt_id in inlet.text


def test_live_and_frozen_completeness_do_not_reuse_old_attempt(session, tmp_path):
    task, run, attempts = _task(session)
    report, metrics = _outputs(session, tmp_path, task, run, attempts[0], flow=1.0)
    check = _check(session, run, attempts[0], report)
    bundle = BundleRow(bundle_id="synthetic-bundle", task_id=task.task_id, revision=1, manifest=[], bundle_digest="d" * 64)

    live = compute_completeness(session, task.task_id, 1)
    frozen = compute_completeness_manifest(session, bundle, [run], [report, metrics], [{
        "run_id": run.run_id, "attempt_id": check.attempt_id, "verification_id": check.verification_id,
    }])

    for result in (live, frozen):
        assert result["complete"] is False
        assert {item["kind"] for item in result["missing"]} >= {"MISSING_RAW_REPORT", "MISSING_VERIFICATION"}


def test_bundle_keeps_history_and_raw_provenance_with_distinct_metrics_role(session, tmp_path):
    task, run, attempts = _task(session)
    _preparation(session, tmp_path, task)
    all_outputs = []
    for index, attempt in enumerate(attempts, start=1):
        report, metrics = _outputs(session, tmp_path, task, run, attempt, flow=float(index))
        _check(session, run, attempt, report)
        all_outputs.extend([report, metrics])
    extra_sources = [
        _artifact(session, tmp_path, task, f"runs/{run.run_id}/attempt-2/{name}", b"synthetic source\n", run=run, attempt=attempts[1])
        for name in ("solver.mock.log", "result.mock.tar", "conversion-evidence.mock.json", "exit-proof.mock.json")
    ]

    bundle = build_bundle(session, task.task_id, revision=1, artifact_root=tmp_path / "bundle-artifacts")
    entries = {entry["artifact_id"]: entry for entry in bundle.manifest}

    assert len(entries) == len(bundle.manifest)
    assert {art.artifact_id for art in all_outputs + extra_sources} <= entries.keys()
    assert all(entries[art.artifact_id]["role"] == "raw" for art in extra_sources)
    assert entries[all_outputs[1].artifact_id]["role"] == "metrics"
    claims = session.query(ClaimRow).filter_by(bundle_id=bundle.bundle_id).all()
    assert claims and {claim.artifact_id for claim in claims} == {all_outputs[3].artifact_id}


@pytest.mark.parametrize("collision", ["other_task", "other_preparation", "other_sha"])
def test_same_project_logical_path_cannot_contaminate_preparation_mode(session, tmp_path, collision):
    task, run, attempts = _task(session)
    _outputs(session, tmp_path, task, run, attempts[1], flow=2.0, mode="REAL")
    prep, job, legitimate = _preparation(session, tmp_path, task, mode="REAL")
    other_job = JobRow(
        job_id=_id("job"), kind="PREPARE",
        task_id="unrelated-task" if collision == "other_task" else task.task_id,
    )
    session.add(other_job)
    session.flush()
    session.add(EventRow(
        event_id=_id("event"), job_id=other_job.job_id, event_seq=1, kind="STARTING",
        payload={"preparation_id": "other-preparation" if collision == "other_preparation" else prep.preparation_id},
    ))
    content = b"different bytes\n" if collision == "other_sha" else Path(legitimate.storage_path).read_bytes()
    _artifact(session, tmp_path, task, legitimate.logical_path, content, job_id=other_job.job_id)

    assert bundle_evidence_mode(session, task.task_id, 1) == "REAL"
    session.delete(legitimate)
    session.flush()
    assert bundle_evidence_mode(session, task.task_id, 1) == "UNKNOWN"


def test_real_method_mismatch_blocks_while_mock_diagnostic_is_explicit(session, tmp_path):
    task, run, attempts = _task(session)  # TaskSpec intentionally declares a synthetic digest.
    _preparation(session, tmp_path, task, mode="REAL")
    report, _ = _outputs(session, tmp_path, task, run, attempts[1], flow=2.0, mode="REAL")
    _check(session, run, attempts[1], report)

    with pytest.raises(ApiError, match="方法摘要"):
        build_bundle(session, task.task_id, revision=1, artifact_root=tmp_path / "bundle-artifacts")

    # These are policy fixtures; changing their tags is not solver evidence.
    for art in session.query(ArtifactRow).all():
        art.evidence_mode = "MOCK"
    session.flush()
    bundle = build_bundle(session, task.task_id, revision=1, artifact_root=tmp_path / "bundle-artifacts")
    summary_entry = next(entry for entry in bundle.manifest if entry["role"] == "summary")
    summary_art = session.get(ArtifactRow, summary_entry["artifact_id"])
    summary = json.loads(Path(summary_art.storage_path).read_text().split("\n", 1)[1])
    assert summary["evidence_mode"] == "MOCK"
    assert summary["method_package"]["digest_match"] is False


def test_all_indexed_method_sources_are_frozen(session, tmp_path, monkeypatch):
    root = tmp_path / "capabilities"
    package = root / "synthetic_flow" / "0.1.0"
    package.mkdir(parents=True)
    sources = {
        "metric-definitions.json": {"metrics": [{"metric_id": "boundary_mass_flow", "unit": "kg/s", "tolerance": None}]},
        "rules.json": {"status": "DRAFT", "rules": [{"rule_id": "SYNTHETIC", "threshold": None}]},
        "domain.json": {"applicability": {}},
        "boundary-map.json": {"synthetic_policy_fixture": True, "roles": ["inlet", "outlet"]},
    }
    source_bytes = {name: json.dumps(value).encode() for name, value in sources.items()}
    for name, content in source_bytes.items():
        (package / name).write_bytes(content)
    manifest = {
        "capability_package_id": "synthetic_flow", "version": "0.1.0", "status": "DRAFT",
        "content_sha256": {name: hashlib.sha256(content).hexdigest() for name, content in source_bytes.items()},
    }
    (package / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setenv("DSH_SIM_CAPABILITIES_ROOT", str(root))
    task, run, attempts = _task(session, method={
        "capability_package_id": "synthetic_flow",
        "capability_package_sha256": sha256_hex(canonical_dumps(manifest)),
        "required_metrics": ["boundary_mass_flow@inlet"], "review_scope": "synthetic policy fixture",
    })
    _preparation(session, tmp_path, task, mode="REAL")
    report, _ = _outputs(session, tmp_path, task, run, attempts[1], flow=2.0, mode="REAL")
    _check(session, run, attempts[1], report)

    bundle = build_bundle(session, task.task_id, revision=1, artifact_root=tmp_path / "bundle-artifacts")

    for name, expected in source_bytes.items():
        entry = next(entry for entry in bundle.manifest if entry["logical_path"].endswith(f"/capability/{name}"))
        artifact = session.get(ArtifactRow, entry["artifact_id"])
        assert Path(artifact.storage_path).read_bytes() == expected
    boundary_entry = next(entry for entry in bundle.manifest if entry["logical_path"].endswith("/boundary-map.json"))
    assert boundary_entry["role"] == "method_source"
