"""Contract/safety checks plus explicitly selected REAL OpenFOAM regressions.

Run the real section with a sourced supported runtime and ``-m real_solver``.
No fake solver output is used. Missing runtime is NOT_RUN, never a REAL pass.
"""
from __future__ import annotations

import io
import json
import math
import shutil
import tarfile
import time
from dataclasses import replace
from pathlib import Path

import pytest

from dsh_sim.adapters.openfoam_adapter import (
    OpenFoamAdapter, make_channel_template, read_template_metadata, template_sha256,
)
from dsh_sim.adapters.star_adapter import ExecutionBudget, WhitelistWrite


def _setup(tmp_path: Path, **params):
    templates = tmp_path / "templates"
    template = make_channel_template(templates / "public.tar", **params)
    adapter = OpenFoamAdapter(template_registry={"art_public": template},
        template_root=templates, allowed_work_root=tmp_path / "work")
    return adapter, template


@pytest.mark.mock
class TestOpenFoamTemplateSafety:
    def test_template_is_byte_reproducible_and_contains_no_engineering_acceptance(self, tmp_path):
        one = make_channel_template(tmp_path / "one.tar")
        two = make_channel_template(tmp_path / "other/path/two.tar")
        assert one.read_bytes() == two.read_bytes()
        meta = read_template_metadata(one)
        with tarfile.open(one) as archive:
            reference = json.load(archive.extractfile("reference.json"))
            assert reference["engineering_acceptance_thresholds"] is None
            assert "Allrun" not in archive.getnames()
            assert "functions {}" in archive.extractfile("system/controlDict").read().decode()
        assert len(meta["boundary_map_sha256"]) == 64

    def test_preparation_freezes_separate_archive_without_mutating_template(self, tmp_path):
        adapter, template = _setup(tmp_path)
        digest = template_sha256(template)
        prepared = adapter.prepare_case("art_public", [WhitelistWrite(
            "mean_velocity", "inlet", .012, "m/s", "public-input")], str(tmp_path / "work/prep"))
        assert template_sha256(template) == digest == prepared.source_template_sha256
        assert Path(prepared.prepared_sim_path).is_file()
        assert prepared.prepared_sha256 != digest
        assert read_template_metadata(prepared.prepared_sim_path)["parameters"]["mean_velocity"] == .012

    def test_artifact_ids_are_an_allowlist_not_filesystem_paths(self, tmp_path):
        adapter, template = _setup(tmp_path)
        with pytest.raises(ValueError, match="allowlist"):
            adapter.inspect_template(str(template))
        with pytest.raises(ValueError, match="outside"):
            OpenFoamAdapter(template_registry={"outside": template},
                template_root=tmp_path / "unrelated", allowed_work_root=tmp_path)

    def test_template_registration_detects_later_source_mutation(self, tmp_path):
        adapter, path = _setup(tmp_path)
        path.chmod(0o644)
        data = make_channel_template(tmp_path / "changed.tar", mean_velocity=.02).read_bytes()
        path.write_bytes(data)
        with pytest.raises(ValueError, match="changed"):
            adapter.inspect_template("art_public")

    @pytest.mark.parametrize("entry_type,name", [(tarfile.REGTYPE, "../escape"),
                                               (tarfile.SYMTYPE, "0/U")])
    def test_unsafe_archive_entries_are_rejected_before_extraction(self, tmp_path, entry_type, name):
        target = tmp_path / "unsafe.tar"
        with tarfile.open(target, "w") as archive:
            entry = tarfile.TarInfo(name)
            entry.type, entry.linkname = entry_type, "/etc/passwd"
            archive.addfile(entry, io.BytesIO())
        with pytest.raises(ValueError, match="Unsafe"):
            read_template_metadata(target)
        assert not (tmp_path.parent / "escape").exists()

    def test_dictionary_injection_is_rejected_even_with_updated_hashes(self, tmp_path):
        import hashlib
        source = make_channel_template(tmp_path / "source.tar")
        with tarfile.open(source) as archive:
            files = {entry.name: archive.extractfile(entry).read() for entry in archive}
        files["system/controlDict"] += b'\nfunctions { attacker { type coded; codeExecute #{ system("id"); #}; } }\n'
        meta = json.loads(files["dsh-openfoam.json"])
        meta["files"]["system/controlDict"] = hashlib.sha256(files["system/controlDict"]).hexdigest()
        files["dsh-openfoam.json"] = json.dumps(meta).encode()
        target = tmp_path / "injected.tar"
        with tarfile.open(target, "w") as archive:
            for name, data in files.items():
                entry = tarfile.TarInfo(name)
                entry.size = len(data)
                archive.addfile(entry, io.BytesIO(data))
        with pytest.raises(ValueError, match="approved numeric recipe"):
            read_template_metadata(target)

    @pytest.mark.parametrize("write", [
        WhitelistWrite("anything", "inlet", .01, "m/s", "test"),
        WhitelistWrite("mean_velocity", "outlet", .01, "m/s", "test"),
        WhitelistWrite("mean_velocity", "inlet", .01, "km/h", "test"),
        WhitelistWrite("mean_velocity", "inlet", float("nan"), "m/s", "test"),
    ])
    def test_unapproved_writes_do_not_create_prepared_files(self, tmp_path, write):
        adapter, _ = _setup(tmp_path)
        with pytest.raises(ValueError):
            adapter.prepare_case("art_public", [write], str(tmp_path / "work/prep"))
        assert not (tmp_path / "work/prep/prepared.openfoam.tar").exists()

    def test_work_path_cannot_escape_node_root(self, tmp_path):
        adapter, _ = _setup(tmp_path)
        with pytest.raises(ValueError, match="outside"):
            adapter.prepare_case("art_public", [], str(tmp_path / "outside"))


def _real_start(tmp_path: Path, *, seconds: int = 30, **params):
    if not all(shutil.which(name) for name in ("blockMesh", "simpleFoam", "foamDictionary", "foamToVTK")):
        pytest.skip("NOT_RUN: source the supported real OpenFOAM v1912 runtime")
    adapter, template = _setup(tmp_path, **params)
    adapter.probe_environment()
    prepared = adapter.prepare_case("art_public", [WhitelistWrite("mean_velocity", "inlet",
        params.get("mean_velocity", .01), "m/s", "public-test-fixture")], str(tmp_path / "work/prep"))
    readback = adapter.read_actual_settings(prepared.prepared_sim_path)
    assert not readback.missing_fields and not readback.mismatched_fields
    # Repeat independently; no mutation of source or readback case is needed.
    assert adapter.read_actual_settings(prepared.prepared_sim_path).readback_sha == readback.readback_sha
    work = tmp_path / "work/run_public/attempt-1"
    work.mkdir(parents=True)
    staged = work / "prepared.openfoam.tar"
    shutil.copyfile(prepared.prepared_sim_path, staged)
    handle = adapter.launch(str(staged), ExecutionBudget(None, seconds, 1, 1))
    return adapter, template, handle


def _wait(adapter, handle, *, limit=35):
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        state = adapter.poll(handle)
        if state.phase in {"SUCCEEDED", "FAILED", "CANCELLED", "LOST"}:
            return state
        time.sleep(.03)
    adapter.cancel(handle)
    pytest.fail("Actual OpenFOAM process did not reach a bounded terminal state")


@pytest.mark.real_solver
class TestOpenFoamRealRuntime:
    def test_real_boundary_evidence_survives_adapter_recreation(self, tmp_path):
        adapter, template, handle = _real_start(tmp_path)
        reconnected = OpenFoamAdapter(template_registry={"art_public": template},
            template_root=tmp_path / "templates", allowed_work_root=tmp_path / "work")
        state = _wait(reconnected, handle)
        assert state.phase == "SUCCEEDED", state
        assert state.detail["exit_proof"]["process_exited"] is True
        assert state.detail["exit_proof"]["process_group_exited"] is True
        outputs = reconnected.collect_outputs(handle)
        metrics = reconnected.extract_metrics(outputs)
        assert metrics.evidence_mode == "REAL" and not metrics.missing_metrics
        assert metrics.metric_values["boundary_mass_flow@inlet"] < 0
        assert metrics.metric_values["boundary_mass_flow@outlet"] > 0
        assert metrics.metric_values["static_pressure@inlet"] > metrics.metric_values["static_pressure@outlet"]
        evidence = json.loads((Path(handle.work_dir) / "conversion-evidence.json").read_text())
        assert evidence["engineering_acceptance_thresholds"] is None
        assert {row["raw_patch_face_index"] for row in evidence["patches"]["inlet"]["faces"]} == set(range(20))
        assert "case/1000/phi" in evidence["source_artifacts"]
        assert any(name.endswith("/boundary/inlet.vtp") for name in evidence["source_artifacts"])
        assert "environment-probe.json" in outputs.artifact_digests
        assert "logs/simpleFoam.stdout.log" in outputs.artifact_digests
        assert Path(outputs.result_sim).is_file()
        adapter.poll(handle)  # Reap original Popen object as well.

    def test_changed_geometry_and_flow_use_actual_new_samples(self, tmp_path):
        adapter, _, handle = _real_start(tmp_path, mean_velocity=.012, height=.08, length=.75,
                                        nx=60, ny=24, nu=.0015)
        assert _wait(adapter, handle).phase == "SUCCEEDED"
        outputs = adapter.collect_outputs(handle)
        assert outputs.summary["conversion_error"] is None
        metrics = adapter.extract_metrics(outputs)
        assert all(value is not None and math.isfinite(value) for value in metrics.metric_values.values())
        evidence = json.loads((Path(handle.work_dir) / "conversion-evidence.json").read_text())
        assert len(evidence["patches"]["outlet"]["faces"]) == 24
        assert template_sha256(tmp_path / "templates/public.tar") == json.loads(
            (Path(handle.work_dir) / "case/dsh-openfoam.json").read_text())["source_template_sha256"]

    def test_actual_solver_failure_preserves_logs_and_leaves_metrics_missing(self, tmp_path):
        adapter, _, handle = _real_start(tmp_path, test_fault="invalid_div_scheme")
        state = _wait(adapter, handle)
        assert state.phase == "FAILED"
        assert state.detail["exit_code"] != 0
        assert state.detail["stage"] == "simpleFoam"
        outputs = adapter.collect_outputs(handle)
        assert outputs.raw_reports == ()
        assert all(value is None for value in adapter.extract_metrics(outputs).metric_values.values())
        assert "logs/simpleFoam.stderr.log" in outputs.artifact_digests
        assert "openfoam-exit.json" in outputs.artifact_digests
        assert Path(outputs.result_sim).is_file()

    def test_real_wall_clock_timeout_has_exit_proof_and_preserves_partial_outputs(self, tmp_path):
        adapter, _, handle = _real_start(tmp_path, iterations=200000, seconds=1)
        state = _wait(adapter, handle, limit=10)
        assert state.phase == "FAILED"
        assert state.detail["exit_code"] == 124
        assert state.detail["exit_proof"]["process_group_exited"] is True
        outputs = adapter.collect_outputs(handle)
        assert outputs.summary["partial"] is True
        assert "logs/simpleFoam.stdout.log" in outputs.artifact_digests

    def test_cancel_after_solver_starts_confirms_whole_process_group_exit(self, tmp_path):
        adapter, _, handle = _real_start(tmp_path, iterations=200000)
        log = Path(handle.work_dir) / "logs/simpleFoam.stdout.log"
        deadline = time.monotonic()+10
        while time.monotonic() < deadline:
            if log.exists() and "Time = " in log.read_text():
                break
            time.sleep(.02)
        else:
            adapter.cancel(handle)
            pytest.fail("Actual solver did not begin iterations before cancellation test")
        # A forged process identity is rejected before any signal can be sent.
        with pytest.raises(ValueError, match="identity"):
            adapter.cancel(replace(handle, process_identity="{}"))
        state = adapter.cancel(handle)
        assert state.phase == "CANCELLED", state
        assert state.detail["exit_proof"]["process_exited"] is True
        assert state.detail["exit_proof"]["process_group_exited"] is True
        outputs = adapter.collect_outputs(handle)
        assert "cancel-request.json" in outputs.artifact_digests
        assert "logs/simpleFoam.stdout.log" in outputs.artifact_digests
        assert outputs.evidence_mode == "REAL"
