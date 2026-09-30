"""Controlled, serial OpenFOAM v1912 channel adapter using the existing protocol.

The public template is a deterministic archive, not a shell script. Only this
module's numeric Poiseuille recipe is accepted. REAL means actual OpenFOAM
execution/observation; it never means engineering approval. Linux process
identity (boot ID, PID, /proc start ticks and process group) is required.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import time
import uuid
import xml.etree.ElementTree as ET
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from dsh_sim.adapters.star_adapter import (
    BoundaryRoleCandidate, CollectedOutputs, EnvironmentProbe, ExecutionBudget,
    JobHandle, JobStatus, LicenseStatus, PreparedCase, RawMetrics, ReadbackEntry,
    ReadbackSet, TemplateInspection, WhitelistWrite,
)
from dsh_sim.canonical import canonical_dumps, sha256_hex

BUILD = "openfoam-channel-adapter/0.1.0"
TOOLS = ("blockMesh", "checkMesh", "simpleFoam", "foamToVTK", "foamDictionary")
MANIFEST = "dsh-openfoam.json"
REPORT_COLUMNS = ("section", "boundary_role", "sign_convention", "mass_flow_kg_s",
                  "total_pressure_pa", "static_pressure_pa")
FIELDS = {
    "mean_velocity": ("inlet", "m/s", "mean_velocity"),
    "kinematic_viscosity": ("fluid", "m2/s", "nu"),
    "density": ("fluid", "kg/m3", "density"),
}
_NUMBER = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _json_bytes(value: Any) -> bytes:
    return (canonical_dumps(value) + "\n").encode("utf-8")


def _atomic_json(path: Path, value: Any) -> None:
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with tmp.open("xb") as out:
        out.write(_json_bytes(value))
        out.flush()
        os.fsync(out.fileno())
    os.replace(tmp, path)


def _tar_bytes(files: Mapping[str, bytes]) -> bytes:
    """Stable ordering, permissions and timestamps; only regular relative files."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for name in sorted(files):
            part = PurePosixPath(name)
            if part.is_absolute() or ".." in part.parts or "\\" in name:
                raise ValueError("Unsafe archive entry")
            info = tarfile.TarInfo(name)
            info.size = len(files[name])
            info.mode, info.uid, info.gid, info.mtime = 0o644, 0, 0, 0
            archive.addfile(info, io.BytesIO(files[name]))
    return buffer.getvalue()


def _params_valid(params: dict[str, Any]) -> None:
    expected = {"mean_velocity", "length", "height", "width", "nu", "density",
                "nx", "ny", "iterations", "test_fault"}
    if set(params) != expected:
        raise ValueError("Unknown or missing channel parameter")
    for key in ("mean_velocity", "length", "height", "width", "nu", "density"):
        value = params[key]
        if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value <= 0:
            raise ValueError(f"{key} must be a positive finite number")
    for key in ("nx", "ny", "iterations"):
        if type(params[key]) is not int or params[key] < 1:
            raise ValueError(f"{key} must be a positive integer")
    # A serial experimental runner resource limit, not an engineering threshold.
    if params["nx"] * params["ny"] > 100_000 or params["iterations"] > 1_000_000:
        raise ValueError("Channel exceeds this experimental runner's resource budget")
    if params["test_fault"] not in (None, "invalid_div_scheme"):
        raise ValueError("Unknown test fault; arbitrary code is never accepted")


def _foam(cls: str, obj: str, body: str) -> bytes:
    return (f"FoamFile\n{{\n version 2.0;\n format ascii;\n class {cls};\n object {obj};\n}}\n" + body + "\n").encode()


def _case_files(p: dict[str, Any]) -> dict[str, bytes]:
    _params_valid(p)
    ny, nx = p["ny"], p["nx"]
    speed, length, height, width = p["mean_velocity"], p["length"], p["height"], p["width"]
    velocities = "\n".join(
        f"({6 * speed * ((i + .5) / ny) * (1 - (i + .5) / ny) / (1 + .5 / ny**2):.17g} 0 0)"
        for i in range(ny)
    )
    scheme = "notAnOpenFOAMScheme" if p["test_fault"] else "Gauss linearUpwind grad(U)"
    boundary_map = {
        "schema_version": "0.1.0", "region": "fluid", "recipe": "plane-poiseuille-channel",
        "roles": {"inlet": {"patch": "inlet", "type": "patch"},
                  "outlet": {"patch": "outlet", "type": "patch"},
                  "walls": {"patch": "walls", "type": "wall"}},
        "sign_convention": "outward_positive", "approval": "PUBLIC_EXPERIMENT_ONLY",
    }
    files = {
        "0/U": _foam("volVectorField", "U", f"""dimensions [0 1 -1 0 0 0 0];
internalField uniform ({speed:.17g} 0 0);
boundaryField {{
 inlet {{ type fixedValue; value nonuniform List<vector> {ny} ( {velocities} ); }}
 outlet {{ type zeroGradient; }}
 walls {{ type noSlip; }}
 frontAndBack {{ type empty; }}
}}"""),
        "0/p": _foam("volScalarField", "p", """dimensions [0 2 -2 0 0 0 0];
internalField uniform 0;
boundaryField {
 inlet { type zeroGradient; }
 outlet { type fixedValue; value uniform 0; }
 walls { type zeroGradient; }
 frontAndBack { type empty; }
}"""),
        "constant/transportProperties": _foam("dictionary", "transportProperties", f"transportModel Newtonian;\nnu [0 2 -1 0 0 0 0] {p['nu']:.17g};"),
        "constant/turbulenceProperties": _foam("dictionary", "turbulenceProperties", "simulationType laminar;"),
        "system/blockMeshDict": _foam("dictionary", "blockMeshDict", f"""convertToMeters 1;
vertices ((0 0 0) ({length:.17g} 0 0) ({length:.17g} {height:.17g} 0) (0 {height:.17g} 0)
 (0 0 {width:.17g}) ({length:.17g} 0 {width:.17g}) ({length:.17g} {height:.17g} {width:.17g}) (0 {height:.17g} {width:.17g}));
blocks (hex (0 1 2 3 4 5 6 7) ({nx} {ny} 1) simpleGrading (1 1 1));
edges ();
boundary (
 inlet {{ type patch; faces ((0 4 7 3)); }}
 outlet {{ type patch; faces ((1 2 6 5)); }}
 walls {{ type wall; faces ((0 1 5 4) (3 7 6 2)); }}
 frontAndBack {{ type empty; faces ((0 3 2 1) (4 5 6 7)); }}
);
mergePatchPairs ();"""),
        "system/controlDict": _foam("dictionary", "controlDict", f"""application simpleFoam;
startFrom startTime;
startTime 0;
stopAt endTime;
endTime {p['iterations']};
deltaT 1;
writeControl timeStep;
writeInterval {p['iterations']};
purgeWrite 0;
writeFormat ascii;
writePrecision 12;
writeCompression off;
timeFormat general;
timePrecision 12;
runTimeModifiable false;
functions {{}}
"""),
        "system/fvSchemes": _foam("dictionary", "fvSchemes", f"""ddtSchemes {{ default steadyState; }}
gradSchemes {{ default Gauss linear; }}
divSchemes {{ default none; div(phi,U) {scheme}; div((nuEff*dev2(T(grad(U))))) Gauss linear; }}
laplacianSchemes {{ default Gauss linear corrected; }}
interpolationSchemes {{ default linear; }}
snGradSchemes {{ default corrected; }}
wallDist {{ method meshWave; }}"""),
        "system/fvSolution": _foam("dictionary", "fvSolution", """solvers {
 p { solver GAMG; tolerance 1e-10; relTol 0.01; smoother GaussSeidel; }
 U { solver smoothSolver; smoother symGaussSeidel; tolerance 1e-10; relTol 0.01; }
}
SIMPLE { nNonOrthogonalCorrectors 0; }
relaxationFactors { fields { p 0.3; } equations { U 0.7; } }"""),
        "boundary-map.json": _json_bytes(boundary_map),
        "normalization.json": _json_bytes({"density_kg_m3": p["density"], "pressure_kind": "gauge",
            "pressure_source_dimensions": "[0 2 -2 0 0 0 0]", "static_pressure_conversion": "rho*p",
            "density_purpose": "postprocessing of an incompressible solver; not a solved density field"}),
        "reference.json": _json_bytes({"scenario": "public textbook plane Poiseuille flow",
            "analytic_kinematic_dp": 12*p["nu"]*speed*length/height**2,
            "analytic_flow_m3_s": speed*height*width,
            "parameter_values": p, "engineering_acceptance_thresholds": None,
            "note": "Analytic reference only. Never substituted for solver outputs."}),
    }
    return files


def _make_archive(p: dict[str, Any], *, source_sha: str | None = None,
                  writes: list[dict[str, Any]] | None = None) -> bytes:
    files = _case_files(p)
    manifest = {"schema_version": "0.1.0", "kind": "prepared" if source_sha else "template",
        "adapter_build": BUILD, "recipe": "plane-poiseuille-channel", "parameters": p,
        "source_template_sha256": source_sha, "writes_applied": writes or [],
        "boundary_map_sha256": hashlib.sha256(files["boundary-map.json"]).hexdigest(),
        "files": {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}}
    return _tar_bytes({**files, MANIFEST: _json_bytes(manifest)})


def make_channel_template(destination: str | Path, *, mean_velocity: float = .01,
        length: float = 1.0, height: float = .1, width: float = .01, nu: float = .001,
        density: float = 1000.0, nx: int = 80, ny: int = 20, iterations: int = 1000,
        test_fault: str | None = None) -> Path:
    """Create an immutable, reproducible public template (no solver execution)."""
    p = {"mean_velocity": mean_velocity, "length": length, "height": height,
         "width": width, "nu": nu, "density": density, "nx": nx, "ny": ny,
         "iterations": iterations, "test_fault": test_fault}
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = _make_archive(p)
    if path.exists():
        if path.is_symlink() or path.read_bytes() != data:
            raise FileExistsError("Template already exists with different content")
    else:
        with path.open("xb") as out:
            out.write(data)
        path.chmod(0o444)
    return path


def template_sha256(path: str | Path) -> str:
    _load_archive(Path(path))
    return _sha(Path(path))


def read_template_metadata(path: str | Path) -> dict[str, Any]:
    return _load_archive(Path(path))[0]


def _load_archive(path: Path) -> tuple[dict[str, Any], dict[str, bytes]]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 32*1024*1024:
        raise ValueError("Expected a bounded regular template archive")
    files: dict[str, bytes] = {}
    with tarfile.open(path, "r:") as archive:
        for entry in archive:
            part = PurePosixPath(entry.name)
            if (not entry.isfile() or part.is_absolute() or ".." in part.parts
                    or "\\" in entry.name or entry.name in files or entry.size > 8*1024*1024):
                raise ValueError("Unsafe, duplicate or oversized template entry")
            reader = archive.extractfile(entry)
            if reader is None:
                raise ValueError("Unreadable template entry")
            files[entry.name] = reader.read()
    try:
        manifest = json.loads(files[MANIFEST])
        if manifest["schema_version"] != "0.1.0" or manifest["recipe"] != "plane-poiseuille-channel":
            raise ValueError("Unsupported template schema or recipe")
        if manifest["kind"] not in {"template", "prepared"} or manifest["adapter_build"] != BUILD:
            raise ValueError("Unsupported archive kind or adapter format")
        if manifest["kind"] == "prepared":
            if not re.fullmatch(r"[a-f0-9]{64}", manifest.get("source_template_sha256") or ""):
                raise ValueError("Prepared archive lacks source provenance")
        elif manifest.get("source_template_sha256") is not None or manifest.get("writes_applied"):
            raise ValueError("Template archive has contradictory preparation metadata")
        seen = set()
        for write in manifest["writes_applied"]:
            field = write["field_id"]
            if field not in FIELDS or field in seen:
                raise ValueError("Unapproved or duplicated preparation metadata")
            role, unit, parameter = FIELDS[field]
            if write["boundary_role"] != role or write["unit"] != unit or write["value_si"] != manifest["parameters"][parameter]:
                raise ValueError("Preparation write metadata differs from the stored case")
            seen.add(field)
        expected = _case_files(manifest["parameters"])
        if set(files) != set(expected) | {MANIFEST}:
            raise ValueError("Unapproved template file")
        for name, content in expected.items():
            if files[name] != content or manifest["files"].get(name) != hashlib.sha256(content).hexdigest():
                raise ValueError(f"Template is not the approved numeric recipe: {name}")
        if manifest["boundary_map_sha256"] != hashlib.sha256(expected["boundary-map.json"]).hexdigest():
            raise ValueError("Boundary map digest mismatch")
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("Invalid template manifest") from exc
    return manifest, files


def _unpack(files: Mapping[str, bytes], directory: Path, *, allow_identical: bool = False) -> None:
    if directory.exists():
        if allow_identical and not directory.is_symlink():
            existing = {str(p.relative_to(directory)): p.read_bytes() for p in _regular_files(directory)}
            if existing == dict(files):
                return
        raise FileExistsError(f"Case directory already exists: {directory}")
    directory.mkdir(parents=True)
    for name, data in files.items():
        target = directory / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


def _contained(path: Path, root: Path, *, exists: bool = False) -> Path:
    resolved = path.resolve(strict=exists)
    if not resolved.is_relative_to(root.resolve()) or path.is_symlink():
        raise ValueError("Path is outside the configured node root or is a symlink")
    return resolved


def _proc_stat(pid: int) -> dict[str, Any] | None:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
        rest = raw[raw.rfind(")")+2:].split()
        return {"pid": pid, "state": rest[0], "pgid": int(rest[2]), "start_ticks": int(rest[19])}
    except (FileNotFoundError, ProcessLookupError):
        return None


def _identity(pid: int) -> str:
    # Some containers expose host /proc while getpid/Popen use namespace PIDs.
    # Match both the namespace inode and NSpid, never a bare numeric PID.
    namespace = os.readlink("/proc/self/ns/pid")
    host_pid = int(Path("/proc/self/stat").read_text().split(" ", 1)[0]) if pid == os.getpid() else None
    if host_pid is None:
        for path in Path("/proc").iterdir():
            if not path.name.isdigit():
                continue
            try:
                if os.readlink(path / "ns/pid") != namespace:
                    continue
                match = re.search(r"^NSpid:\s+(.+)$", (path / "status").read_text(), re.M)
                if match and int(match.group(1).split()[-1]) == pid:
                    host_pid = int(path.name)
                    break
            except (FileNotFoundError, ProcessLookupError, PermissionError):
                continue
    info = _proc_stat(host_pid) if host_pid is not None else None
    if info is None:
        raise RuntimeError("Process exited before identity was captured")
    return canonical_dumps({"pid": host_pid, "pgid": info["pgid"], "local_pid": pid,
        "local_pgid": os.getpgid(pid), "pid_namespace": namespace, "start_ticks": info["start_ticks"],
        "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip()})


def _identity_live(identity: str) -> bool:
    record = json.loads(identity)
    if record["boot_id"] != Path("/proc/sys/kernel/random/boot_id").read_text().strip():
        return False
    info = _proc_stat(record["pid"])
    return bool(info and info["start_ticks"] == record["start_ticks"]
                and info["pgid"] == record["pgid"] and info["state"] != "Z")


def _group_members(identity: str) -> list[int]:
    record = json.loads(identity)
    if record["boot_id"] != Path("/proc/sys/kernel/random/boot_id").read_text().strip():
        return []
    members = []
    for path in Path("/proc").iterdir():
        if path.name.isdigit():
            info = _proc_stat(int(path.name))
            if info and info["pgid"] == record["pgid"] and info["state"] != "Z":
                members.append(info["pid"])
    return members


class OpenFoamAdapter:
    """Registered templates + configured node roots. No arbitrary command tool."""
    adapter_build = BUILD

    def __init__(self, *, template_registry: Mapping[str, str | Path] | None = None,
                 template_root: str | Path | None = None,
                 allowed_work_root: str | Path | None = None) -> None:
        self.template_root = Path(template_root or os.environ.get("DSH_SIM_OPENFOAM_TEMPLATE_ROOT", "var/openfoam/templates")).resolve()
        self.work_root = Path(allowed_work_root or os.environ.get("DSH_SIM_WORKER_WORK_DIR", "var/worker")).resolve()
        registry = template_registry
        if registry is None:
            configured = os.environ.get("DSH_SIM_OPENFOAM_TEMPLATE_REGISTRY")
            registry = json.loads(Path(configured).read_text()) if configured else {}
        self.templates: dict[str, tuple[Path, str]] = {}
        for ref, candidate in registry.items():
            path = _contained(Path(candidate), self.template_root, exists=True)
            self.templates[ref] = (path, template_sha256(path))
        self.executables = {name: str(Path(found).resolve()) for name in TOOLS if (found := shutil.which(name))}
        self._processes: dict[str, subprocess.Popen] = {}
        self.adapter_build = BUILD + "+sha256:" + _sha(Path(__file__))

    def _tool(self, name: str) -> str:
        if name not in self.executables:
            raise RuntimeError(f"NOT_RUN: source the supported OpenFOAM runtime; {name} unavailable")
        return self.executables[name]

    def _template(self, ref: str) -> tuple[Path, str]:
        if ref not in self.templates:
            raise ValueError("Template artifact is not in the configured allowlist")
        path, digest = self.templates[ref]
        _contained(path, self.template_root, exists=True)
        if template_sha256(path) != digest:
            raise ValueError("Registered source template changed")
        return path, digest

    def probe_environment(self) -> EnvironmentProbe:
        if not Path("/proc/sys/kernel/random/boot_id").is_file():
            raise RuntimeError("NOT_RUN: this adapter's process identity support requires Linux /proc")
        observed = {}
        for tool in TOOLS:
            result = subprocess.run([self._tool(tool), "-help"], shell=False, capture_output=True, text=True, timeout=15)
            if result.returncode:
                raise RuntimeError(f"OpenFOAM probe {tool} failed: {result.stderr[-400:]}")
            observed[tool] = result.stdout + result.stderr
        text = observed["simpleFoam"]
        if "1912" not in text:
            raise RuntimeError("NOT_RUN: only the observed OpenFOAM v1912 runtime is supported")
        build = "; ".join(line.strip() for line in text.splitlines() if line.startswith(("Using:", "Build:", "Arch:")))
        return EnvironmentProbe(star_build=build, executable_path=self._tool("simpleFoam"),
            version_features=tuple(TOOLS), license_status=LicenseStatus.LICENSED,
            license_details={"kind": "open-source OpenFOAM", "vendor_token_required": False,
                "probe": "Each named executable returned exit 0 for -help", "execution_scope": "serial Linux v1912",
                "executables": {name: {"path": path, "sha256": _sha(Path(path))} for name, path in self.executables.items()},
                "observed_help": observed, "adapter_build": self.adapter_build},
            probed_at=_now(), evidence_mode="REAL")

    def inspect_template(self, template_ref: str) -> TemplateInspection:
        path, digest = self._template(template_ref)
        metadata = read_template_metadata(path)
        p = metadata["parameters"]
        return TemplateInspection(template_ref=template_ref,
            boundary_role_candidates=tuple(BoundaryRoleCandidate(name, typ, "fluid", {})
                for name, typ in (("inlet", "patch"), ("outlet", "patch"), ("walls", "wall"))),
            regions=("fluid",), physics_models=("incompressible", "laminar", "Newtonian"),
            mesh_summary={"planned_cells": p["nx"]*p["ny"], "template_sha256": digest,
                "boundary_map_sha256": metadata["boundary_map_sha256"],
                "inspection": "fixed-recipe dictionary inspection; mesh not executed here"},
            report_definitions=("boundary_mass_flow", "static_pressure", "total_pressure"),
            class_name_guess_warning="Public numeric recipe inspected. Boundary mesh geometry becomes measured only after blockMesh.",
            evidence_mode="REAL")

    def prepare_case(self, template_ref: str, whitelist_writes: list[WhitelistWrite], work_dir: str) -> PreparedCase:
        source, digest = self._template(template_ref)
        metadata = read_template_metadata(source)
        p = dict(metadata["parameters"])
        seen: set[str] = set()
        for write in whitelist_writes:
            if write.field_id in seen or write.field_id not in FIELDS:
                raise ValueError("Unapproved or duplicate condition field")
            role, unit, key = FIELDS[write.field_id]
            if write.boundary_role != role or write.unit != unit:
                raise ValueError("Condition role or SI unit differs from the fixed contract")
            p[key] = write.value_si
            seen.add(write.field_id)
        _params_valid(p)
        work = _contained(Path(work_dir), self.work_root)
        work.mkdir(parents=True, exist_ok=True)
        prepared = work / "prepared.openfoam.tar"
        data = _make_archive(p, source_sha=digest, writes=[asdict(w) for w in whitelist_writes])
        with prepared.open("xb") as out:
            out.write(data)
        prepared.chmod(0o444)
        if _sha(source) != digest:
            raise RuntimeError("Source template was changed during preparation")
        return PreparedCase(str(prepared), _sha(prepared), digest, tuple(whitelist_writes),
            {"evidence_mode": "REAL", "recipe": "plane-poiseuille-channel", "parameters": p,
                "engineering_acceptance": "NOT_CHECKED", "original_immutable": True}, "REAL")

    def read_actual_settings(self, prepared_ref: str) -> ReadbackSet:
        prepared = _contained(Path(prepared_ref), self.work_root, exists=True)
        manifest, files = _load_archive(prepared)
        readback_dir = prepared.parent / "readback-case"
        _unpack(files, readback_dir, allow_identical=True)
        queries = {"mean_velocity": ("0/U", "boundaryField.inlet.value"),
                   "kinematic_viscosity": ("constant/transportProperties", "nu")}
        actual: dict[str, float | None] = {}
        proofs = []
        for field, (file, entry) in queries.items():
            argv = [self._tool("foamDictionary"), str(readback_dir / file), "-disableFunctionEntries",
                    "-precision", "17", "-entry", entry, "-value"]
            result = subprocess.run(argv, shell=False, capture_output=True, text=True, timeout=15)
            log = prepared.parent / f"readback-{field}.log"
            log.write_text(result.stdout + result.stderr)
            if result.returncode:
                actual[field] = None
            elif field == "mean_velocity":
                vectors = _vectors(result.stdout)
                actual[field] = math.fsum(v[0] for v in vectors)/len(vectors) if vectors else None
            else:
                values = re.findall(_NUMBER, result.stdout)
                actual[field] = float(values[-1]) if values else None
            proofs.append({"field": field, "argv": argv, "returncode": result.returncode, "log_sha256": _sha(log)})
        actual["density"] = json.loads(files["normalization.json"])["density_kg_m3"]
        entries = []
        for write in manifest["writes_applied"]:
            value = actual.get(write["field_id"])
            requested = write["value_si"]
            # Floating representation comparison, not an engineering tolerance.
            match = value is not None and math.isclose(value, requested, rel_tol=1e-13, abs_tol=0)
            entries.append(ReadbackEntry(write["field_id"], requested, value,
                "MATCH" if match else ("MISSING" if value is None else "MISMATCH")))
        record = {"entries": [asdict(e) for e in entries], "proofs": proofs,
                  "normalization_sha256": hashlib.sha256(files["normalization.json"]).hexdigest(),
                  "density_scope": "independently reread postprocessing input, not a solved density field"}
        _atomic_json(prepared.parent / "readback-evidence.json", record)
        return ReadbackSet(tuple(entries), tuple(e.field_id for e in entries if e.status == "MISSING"),
            tuple(e.field_id for e in entries if e.status == "MISMATCH"),
            {"solver": "simpleFoam", "flow": "laminar incompressible", "density": "postprocess constant"},
            sha256_hex(canonical_dumps(record)), "REAL")

    def launch(self, prepared_ref: str, budget: ExecutionBudget) -> JobHandle:
        prepared = _contained(Path(prepared_ref), self.work_root, exists=True)
        manifest, files = _load_archive(prepared)
        if manifest["kind"] != "prepared":
            raise ValueError("launch requires a prepared, readback-compatible archive")
        if budget.cpu_cores != 1 or budget.attempt_no < 1:
            raise ValueError("Only an explicitly budgeted single CPU serial run is supported")
        if budget.wall_clock_seconds is None or budget.wall_clock_seconds <= 0:
            raise ValueError("A positive wall-clock budget is required")
        work = prepared.parent
        if work.name != f"attempt-{budget.attempt_no}":
            raise ValueError("Attempt work directory does not match the execution budget")
        if (work / "openfoam-job.json").exists():
            raise FileExistsError("An attempt cannot be launched twice")
        case = work / "case"
        _unpack(files, case)
        iterations = manifest["parameters"]["iterations"]
        if budget.max_iterations is not None:
            if budget.max_iterations < 1:
                raise ValueError("max_iterations must be positive")
            iterations = min(iterations, budget.max_iterations)
            text = (case / "system/controlDict").read_text()
            text = re.sub(r"\bendTime\s+\d+;", f"endTime {iterations};", text)
            text = re.sub(r"\bwriteInterval\s+\d+;", f"writeInterval {iterations};", text)
            (case / "system/controlDict").write_text(text)
        for name in TOOLS:
            self._tool(name)
        probe = self.probe_environment()
        _atomic_json(work / "environment-probe.json", asdict(probe))
        job_id = "openfoam-" + uuid.uuid4().hex
        job = {"schema_version": "0.1.0", "job_id": job_id, "run_id": work.parent.name,
            "attempt_no": budget.attempt_no, "work_dir": str(work), "case_dir": str(case),
            "prepared_sha256": _sha(prepared), "adapter_build": self.adapter_build, "evidence_mode": "REAL",
            "budget": asdict(budget), "effective_iterations": iterations,
            "executables": self.executables,
            "executable_sha256": {name: _sha(Path(path)) for name, path in self.executables.items()}, "created_at": _now()}
        _atomic_json(work / "openfoam-job.json", job)
        argv = [sys.executable, "-m", "dsh_sim.adapters.openfoam_adapter", "--run-job", str(work / "openfoam-job.json")]
        proc = None
        try:
            env = os.environ.copy()
            package_root = str(Path(__file__).resolve().parents[2])
            env["PYTHONPATH"] = package_root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
            with (work / "supervisor.stdout.log").open("xb") as out, (work / "supervisor.stderr.log").open("xb") as err:
                proc = subprocess.Popen(argv, shell=False, cwd=work, stdout=out, stderr=err,
                    stdin=subprocess.DEVNULL, start_new_session=True, env=env)
            identity = _identity(proc.pid)
            _atomic_json(work / "openfoam-process.json", {"process_identity": identity, "launch_args": argv})
            self._processes[job_id] = proc
            return JobHandle(job_id, work.parent.name, budget.attempt_no, identity, str(work), tuple(argv))
        except Exception:
            if proc is not None and proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=10)
            _atomic_json(work / "launch-error.json", {"time": _now(), "reason": "launch failed before returning handle",
                "returncode": proc.returncode if proc else None, "process_exited": proc is None or proc.poll() is not None})
            raise

    def _job(self, handle: JobHandle) -> tuple[Path, dict[str, Any]]:
        work = _contained(Path(handle.work_dir), self.work_root, exists=True)
        job = json.loads((work / "openfoam-job.json").read_text())
        record = json.loads((work / "openfoam-process.json").read_text())
        if (job["job_id"] != handle.job_id or job["run_id"] != handle.run_id
                or job["attempt_no"] != handle.attempt_no or record["process_identity"] != handle.process_identity):
            raise ValueError("Job handle identity differs from durable job records")
        return work, job

    def poll(self, job_handle: JobHandle) -> JobStatus:
        work, job = self._job(job_handle)
        proc = self._processes.get(job_handle.job_id)
        if proc is not None:
            proc.poll()  # reap our child; restart polling does not require this Popen object
        identity = job_handle.process_identity
        assert identity is not None
        exit_path = work / "openfoam-exit.json"
        exit_record = json.loads(exit_path.read_text()) if exit_path.is_file() else {}
        stage_path = work / "openfoam-stage.json"
        current = json.loads(stage_path.read_text()) if stage_path.is_file() else {}
        members = _group_members(identity)
        live = _identity_live(identity)
        proof = {"process_exited": not live, "process_group_exited": not members,
            "returncode": exit_record.get("returncode"), "process_identity": identity,
            "evidence": "Linux boot ID + PID start ticks + process group scan; zombie processes excluded",
            "remaining_pids": members}
        if exit_record and exit_record.get("process_identity") != identity:
            phase = "LOST"
            proof["process_exited"] = False
        elif exit_record and not members:
            phase = exit_record["phase"]
        elif live:
            phase = "RUNNING"
        else:
            phase = "LOST"
        return JobStatus(job_handle.job_id, phase, _now(), str(work / "logs/simpleFoam.stdout.log"),
            {"evidence_mode": "REAL", "exit_code": exit_record.get("returncode"),
                "exit_proof": proof, "stage": exit_record.get("stage") or current.get("stage"),
                "current_command": current.get("argv"), "job_record": str(work / "openfoam-job.json")})

    def cancel(self, job_handle: JobHandle) -> JobStatus:
        work, _ = self._job(job_handle)
        before = self.poll(job_handle)
        if before.phase in {"SUCCEEDED", "FAILED", "CANCELLED", "LOST"}:
            return before
        identity = job_handle.process_identity
        assert identity is not None
        namespace_record = json.loads(identity)
        if namespace_record.get("pid_namespace") != os.readlink("/proc/self/ns/pid"):
            return JobStatus(job_handle.job_id, "LOST", _now(), before.log_location,
                {**before.detail, "reason": "Cancellation cannot signal a process in another PID namespace"})
        _atomic_json(work / "cancel-request.json", {"requested_at": _now(), "process_identity": identity})
        if not _identity_live(identity):
            return self.poll(job_handle)
        try:
            os.killpg(namespace_record["local_pgid"], signal.SIGTERM)
        except ProcessLookupError:
            return self.poll(job_handle)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            result = self.poll(job_handle)
            if result.phase == "LOST" and result.detail["exit_proof"]["process_exited"] and result.detail["exit_proof"]["process_group_exited"]:
                proc = self._processes.get(job_handle.job_id)
                observed = proc.poll() if proc else None
                _atomic_json(work / "openfoam-exit.json", {"phase": "CANCELLED", "returncode": observed,
                    "stage": "cancel", "process_identity": identity, "finished_at": _now(),
                    "reason": "exit observed after sending SIGTERM to the verified process group"})
                return self.poll(job_handle)
            if result.phase in {"CANCELLED", "SUCCEEDED", "FAILED", "LOST"}:
                return result
            time.sleep(.025)
        # Escalate only while the same anchored process is still alive.
        if _identity_live(identity):
            os.killpg(namespace_record["local_pgid"], signal.SIGKILL)
        deadline = time.monotonic() + 3
        while _group_members(identity) and time.monotonic() < deadline:
            proc = self._processes.get(job_handle.job_id)
            if proc is not None:
                proc.poll()
            time.sleep(.025)
        if not _identity_live(identity) and not _group_members(identity):
            _atomic_json(work / "openfoam-exit.json", {"phase": "CANCELLED", "returncode": -signal.SIGKILL,
                "stage": "cancel", "process_identity": identity, "finished_at": _now(),
                "reason": "cancel escalated; process group exit observed"})
        return self.poll(job_handle)

    def collect_outputs(self, job_handle: JobHandle) -> CollectedOutputs:
        work, job = self._job(job_handle)
        status = self.poll(job_handle)
        sources: tuple[str, ...] = ()
        conversion_error = None
        if not _group_members(job_handle.process_identity or "{}"):
            try:
                report = _convert_boundary_outputs(work / "case", work)
                sources = (str(report),)
            except (ValueError, OSError, ET.ParseError) as exc:
                conversion_error = str(exc)
                _atomic_json(work / "conversion-missing.json", {"evidence_mode": "REAL", "reason": conversion_error,
                    "policy": "Missing observations remain missing. No analytic replacement."})
        monitor = _residual_csv(work)
        complete = bool(status.detail["exit_proof"]["process_group_exited"])
        case_files = _regular_files(work / "case")
        result = work / "result.openfoam.tar" if complete else None
        if result is not None:
            result.write_bytes(_tar_bytes({str(path.relative_to(work / "case")): path.read_bytes() for path in case_files}))
        artifacts = {str(path.relative_to(work)): _sha(path) for path in _regular_files(work)
                     if not path.name.endswith(".tmp") and "/readback-case/" not in str(path)}
        return CollectedOutputs(job_handle.run_id, job_handle.attempt_no, sources,
            (str(monitor),) if monitor else (), (), str(result) if result else None, artifacts,
            {"evidence_mode": "REAL", "phase": status.phase, "partial": status.phase != "SUCCEEDED",
                "conversion_error": conversion_error, "exit_proof": status.detail["exit_proof"],
                "engineering_acceptance": "NOT_CHECKED", "adapter_build": self.adapter_build}, "REAL")

    def extract_metrics(self, collected: CollectedOutputs) -> RawMetrics:
        from dsh_sim.verify.extract import parse_report_csv
        values: dict[str, float | None] = {}
        hashes = {}
        for name in collected.raw_reports:
            rows, _ = parse_report_csv(name)
            for row in rows:
                for prefix, value in (("boundary_mass_flow", row.mass_flow_kg_s),
                                      ("total_pressure", row.total_pressure_pa),
                                      ("static_pressure", row.static_pressure_pa)):
                    key = f"{prefix}@{row.boundary_role}"
                    values[key] = value
                    hashes[key] = _sha(Path(name))
        for role in ("inlet", "outlet"):
            for prefix in ("boundary_mass_flow", "total_pressure", "static_pressure"):
                values.setdefault(f"{prefix}@{role}", None)
        return RawMetrics(values, tuple(k for k, v in values.items() if v is None),
            "openfoam-v1912-patch-extractor", "0.1.0", hashes, "REAL")


def _regular_files(root: Path) -> list[Path]:
    files = []
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError("Unexpected symlink in solver output")
        if path.is_file():
            files.append(path)
    return sorted(files)


def _strip_comments(text: str) -> str:
    return re.sub(r"//[^\n]*|/\*.*?\*/", "", text, flags=re.S)


def _block(text: str, name: str) -> str:
    match = re.search(r"\b" + re.escape(name) + r"\s*\{", text)
    if not match:
        raise ValueError(f"Missing dictionary block {name}")
    depth, start = 1, match.end()
    for i in range(start, len(text)):
        depth += (text[i] == "{") - (text[i] == "}")
        if depth == 0:
            return text[start:i]
    raise ValueError("Unclosed dictionary block")


def _vectors(text: str) -> list[tuple[float, float, float]]:
    return [tuple(float(x) for x in match) for match in re.findall(
        r"\(\s*("+_NUMBER+r")\s+("+_NUMBER+r")\s+("+_NUMBER+r")\s*\)", text)]


def _list_body(text: str) -> tuple[int, str]:
    text = _strip_comments(text)
    header = _block(text, "FoamFile")
    start = text.index(header) + len(header) + 1
    match = re.search(r"\b(\d+)\s*\((.*)\)\s*$", text[start:], flags=re.S)
    if not match:
        raise ValueError("Expected an ASCII OpenFOAM counted list")
    return int(match.group(1)), match.group(2)


def _mesh_patch(case: Path, patch: str) -> list[tuple[float, float, float]]:
    mesh = case / "constant/polyMesh"
    count, body = _list_body((mesh / "points").read_text())
    points = _vectors(body)
    if len(points) != count:
        raise ValueError("Mesh point count differs from its header")
    count, body = _list_body((mesh / "faces").read_text())
    faces = []
    for number, content in re.findall(r"(\d+)\s*\(([^()]*)\)", body):
        ids = [int(x) for x in content.split()]
        if len(ids) != int(number):
            raise ValueError("Mesh face count differs from its header")
        faces.append(ids)
    if len(faces) != count:
        raise ValueError("Mesh face list is incomplete")
    boundary = _block(_strip_comments((mesh / "boundary").read_text()), patch)
    n = int(re.search(r"\bnFaces\s+(\d+)", boundary).group(1))
    start = int(re.search(r"\bstartFace\s+(\d+)", boundary).group(1))
    selected = faces[start:start+n]
    if len(selected) != n:
        raise ValueError("Boundary face addressing exceeds the mesh")
    return [tuple(math.fsum(points[j][axis] for j in ids)/len(ids) for axis in range(3)) for ids in selected]


def _phi_values(field: Path, patch: str, count: int) -> list[float]:
    text = _strip_comments(field.read_text())
    if not re.search(r"dimensions\s*\[\s*0\s+3\s+-1\s+0\s+0\s+0\s+0\s*\]", text):
        raise ValueError("phi does not have volumetric-flux dimensions")
    content = _block(_block(text, "boundaryField"), patch)
    match = re.search(r"value\s+nonuniform\s+List<scalar>\s+(\d+)\s*\((.*?)\)\s*;", content, re.S)
    if match:
        values = [float(x) for x in match.group(2).split()]
        if len(values) != count or int(match.group(1)) != count:
            raise ValueError("phi patch count mismatch")
    else:
        uniform = re.search(r"value\s+uniform\s+("+_NUMBER+r")\s*;", content)
        if not uniform:
            raise ValueError("Actual phi boundary values are unavailable")
        values = [float(uniform.group(1))] * count
    if not all(math.isfinite(x) for x in values):
        raise ValueError("Nonfinite phi sample")
    return values


def _vtk_patch(path: Path) -> tuple[list[dict[str, Any]], float]:
    root = ET.parse(path).getroot()
    piece = root.find("./PolyData/Piece")
    if piece is None:
        raise ValueError("Expected VTK boundary PolyData")
    def array(xpath: str) -> list[float]:
        node = piece.find(xpath)
        if node is None or node.get("format") != "ascii":
            raise ValueError("Missing ASCII VTK sample array")
        values = [float(x) for x in (node.text or "").split()]
        if not all(math.isfinite(x) for x in values):
            raise ValueError("Nonfinite VTK sample")
        return values
    coords = array("./Points/DataArray")
    points = [coords[i:i+3] for i in range(0, len(coords), 3)]
    connectivity = [int(x) for x in array("./Polys/DataArray[@Name='connectivity']")]
    offsets = [int(x) for x in array("./Polys/DataArray[@Name='offsets']")]
    pressures = array("./CellData/DataArray[@Name='p']")
    velocities = array("./CellData/DataArray[@Name='U']")
    if len(pressures) != len(offsets) or len(velocities) != len(offsets)*3:
        raise ValueError("VTK field tuple count does not match patch faces")
    rows, start = [], 0
    for i, end in enumerate(offsets):
        polygon = [points[j] for j in connectivity[start:end]]
        start = end
        if len(polygon) != 4:
            raise ValueError("Fixed channel export requires quadrilateral boundary faces")
        cross = [math.fsum(polygon[j][(k+1)%3]*polygon[(j+1)%4][(k+2)%3]
                          -polygon[j][(k+2)%3]*polygon[(j+1)%4][(k+1)%3] for j in range(4))/2 for k in range(3)]
        area = math.sqrt(math.fsum(x*x for x in cross))
        if area <= 0:
            raise ValueError("Degenerate exported boundary face")
        rows.append({"centroid": [math.fsum(p[k] for p in polygon)/4 for k in range(3)],
                     "area_m2": area, "p_kinematic": pressures[i], "U_m_s": velocities[i*3:i*3+3]})
    sample_time = float(root.find("./PolyData/FieldData/DataArray[@Name='TimeValue']").text)
    return rows, sample_time


def _convert_boundary_outputs(case: Path, work: Path) -> Path:
    times = [(float(p.name), p) for p in case.iterdir() if p.is_dir() and re.fullmatch(_NUMBER, p.name) and float(p.name) > 0]
    if not times:
        raise ValueError("No positive-time solver output")
    final_time, final = max(times)
    for field, dimensions in (("p", "0 2 -2 0 0 0 0"), ("U", "0 1 -1 0 0 0 0")):
        text = (final / field).read_text()
        found = re.search(r"dimensions\s*\[([^]]+)\]", text)
        if not found or " ".join(found.group(1).split()) != dimensions:
            raise ValueError(f"Unexpected dimensions for {field}")
    density = json.loads((case / "normalization.json").read_text())["density_kg_m3"]
    if not math.isfinite(density) or density <= 0:
        raise ValueError("Invalid conversion density")
    params = json.loads((case / MANIFEST).read_text())["parameters"]
    evidence: dict[str, Any] = {"evidence_mode": "REAL", "extractor": "openfoam-v1912-patch-extractor/0.1.0",
        "time": final_time, "density_kg_m3": density, "pressure_kind": "gauge",
        "static_formula": "rho*sum(area_i*p_i)/sum(area_i)",
        "mass_formula": "rho*sum(phi_i), outward positive",
        "total_formula": "rho*sum(abs(phi_i)*(p_i+0.5*dot(U_i,U_i)))/sum(abs(phi_i))",
        "coordinate_tolerance_purpose": "ASCII Float32 mesh-address matching only; not an engineering acceptance threshold",
        "coordinate_tolerance_m": 2e-6*max(params["length"], params["height"], params["width"]),
        "precision_limit": "OpenFOAM v1912 foamToVTK ASCII Float32 output has approximately six significant digits for p/U. Raw phi is retained at solver write precision.",
        "engineering_acceptance_thresholds": None, "patches": {}, "source_artifacts": {}}
    source_paths = [final / "p", final / "U", final / "phi", case / "normalization.json",
        *(case / "constant/polyMesh" / n for n in ("points", "faces", "boundary"))]
    reports = []
    for patch in ("inlet", "outlet"):
        candidates = list((case / "VTK").glob(f"*/boundary/{patch}.vtp"))
        candidates = [p for p in candidates if float(ET.parse(p).getroot().find("./PolyData/FieldData/DataArray[@Name='TimeValue']").text) == final_time]
        if len(candidates) != 1:
            raise ValueError(f"Exactly one official final-time {patch} VTK export is required")
        vtk = candidates[0]
        rows, observed_time = _vtk_patch(vtk)
        centroids = _mesh_patch(case, patch)
        flux = _phi_values(final / "phi", patch, len(centroids))
        if len(rows) != len(centroids):
            raise ValueError("Official export and raw mesh have different face counts")
        unmatched = set(range(len(centroids)))
        for row in rows:
            distances = sorted((math.dist(row["centroid"], centroids[i]), i) for i in unmatched)
            if not distances or distances[0][0] > evidence["coordinate_tolerance_m"]:
                raise ValueError("VTK face cannot be matched to raw phi mesh addressing")
            _, index = distances[0]
            unmatched.remove(index)
            row["raw_patch_face_index"] = index
            row["phi_m3_s"] = flux[index]
        area = math.fsum(row["area_m2"] for row in rows)
        static = density * math.fsum(row["area_m2"]*row["p_kinematic"] for row in rows)/area
        weight = math.fsum(abs(row["phi_m3_s"]) for row in rows)
        expected_sign = -1 if patch == "inlet" else 1
        total = None
        if weight > 0 and all(expected_sign*row["phi_m3_s"] >= 0 for row in rows):
            total = density * math.fsum(abs(row["phi_m3_s"])*(row["p_kinematic"]+
                    .5*math.fsum(v*v for v in row["U_m_s"])) for row in rows)/weight
        mass = density * math.fsum(flux)
        reports.append((patch, patch, "outward_positive", mass, total, static))
        evidence["patches"][patch] = {"faces": rows, "mass_flow_kg_s": mass,
            "static_pressure_pa": static, "total_pressure_pa": total, "sample_time": observed_time}
        source_paths.append(vtk)
    evidence["source_artifacts"] = {str(p.relative_to(work)): _sha(p) for p in source_paths}
    report = work / "report.csv"
    with report.open("w", newline="") as out:
        out.write("# evidence_mode: REAL; derived from actual solver/exported patch samples\n")
        writer = csv.writer(out)
        writer.writerow(REPORT_COLUMNS)
        writer.writerows(reports)
    evidence["normalized_report_sha256"] = _sha(report)
    _atomic_json(work / "conversion-evidence.json", evidence)
    return report


def _residual_csv(work: Path) -> Path | None:
    source = work / "logs/simpleFoam.stdout.log"
    if not source.exists():
        return None
    values, iteration = {}, None
    pattern = re.compile(r"Solving for ([^,]+), Initial residual = ("+_NUMBER+r"), Final residual = ("+_NUMBER+r"), No Iterations (\d+)")
    for line in source.read_text(errors="replace").splitlines():
        match = re.fullmatch(r"Time = ("+_NUMBER+r")", line.strip())
        if match:
            iteration = float(match.group(1))
        match = pattern.search(line)
        if match and iteration is not None:
            values.setdefault(iteration, {})[match.group(1)] = float(match.group(2))
    if not values:
        return None
    target = work / "monitor.csv"
    with target.open("w", newline="") as out:
        out.write("# evidence_mode: REAL; initial linear-solver residuals from logs/simpleFoam.stdout.log; no convergence verdict\n")
        writer = csv.writer(out)
        writer.writerow(("iteration", "p_initial_residual", "Ux_initial_residual", "Uy_initial_residual"))
        writer.writerows((iteration, row.get("p"), row.get("Ux"), row.get("Uy")) for iteration, row in sorted(values.items()))
    return target


def _run_job(job_file: Path) -> int:
    """Fixed serial supervisor, launched as its own process group; no shell."""
    job = json.loads(job_file.read_text())
    work, case = Path(job["work_dir"]), Path(job["case_dir"])
    identity = _identity(os.getpid())
    cancelled = False
    def request_stop(signum: int, frame: Any) -> None:
        nonlocal cancelled
        cancelled = True
    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    logs = work / "logs"
    logs.mkdir()
    tools = job["executables"]
    commands = [("blockMesh", [tools["blockMesh"], "-case", str(case)]),
        ("checkMesh", [tools["checkMesh"], "-case", str(case)]),
        ("simpleFoam", [tools["simpleFoam"], "-case", str(case)]),
        ("foamToVTK", [tools["foamToVTK"], "-case", str(case), "-latestTime", "-ascii",
            "-fields", "(p U)", "-no-internal", "-no-point-data", "-patches", "(inlet outlet)", "-overwrite"])]
    deadline = time.monotonic()+job["budget"]["wall_clock_seconds"]
    records, phase, rc, stage = [], "FAILED", 70, "starting"
    child = None
    try:
        if job["adapter_build"] != BUILD + "+sha256:" + _sha(Path(__file__)):
            raise RuntimeError("Adapter implementation changed after launch intent was recorded")
        for name, path in tools.items():
            if _sha(Path(path)) != job["executable_sha256"][name]:
                raise RuntimeError(f"Executable changed after probe: {name}")
        for stage, argv in commands:
            if cancelled or (work / "cancel-request.json").exists():
                phase, rc = "CANCELLED", 130
                break
            if time.monotonic() >= deadline:
                phase, rc = "FAILED", 124
                break
            record = {"stage": stage, "argv": argv, "started_at": _now()}
            _atomic_json(work / "openfoam-stage.json", record)
            with (logs / f"{stage}.stdout.log").open("xb") as out, (logs / f"{stage}.stderr.log").open("xb") as err:
                child = subprocess.Popen(argv, shell=False, cwd=case, stdout=out, stderr=err, stdin=subprocess.DEVNULL)
                while child.poll() is None:
                    if cancelled or (work / "cancel-request.json").exists() or time.monotonic() >= deadline:
                        child.terminate()
                        try:
                            child.wait(timeout=2)
                        except subprocess.TimeoutExpired:
                            child.kill()
                            child.wait()
                        rc = 130 if cancelled or (work / "cancel-request.json").exists() else 124
                        phase = "CANCELLED" if rc == 130 else "FAILED"
                        break
                    time.sleep(.025)
                else:
                    rc = child.returncode
                if cancelled or (work / "cancel-request.json").exists():
                    phase, rc = "CANCELLED", 130
            record.update({"finished_at": _now(), "returncode": child.returncode, "budget_exit_code": rc})
            records.append(record)
            _atomic_json(work / "execution-commands.json", records)
            if rc:
                break
            if stage == "checkMesh" and "Mesh OK" not in (logs / f"{stage}.stdout.log").read_text():
                rc, phase = 65, "FAILED"
                break
        else:
            phase, rc = "SUCCEEDED", 0
    except Exception as exc:
        rc, phase = 70, "FAILED"
        _atomic_json(work / "supervisor-error.json", {"type": type(exc).__name__, "message": str(exc)})
    finally:
        if child is not None and child.poll() is None:
            child.kill()
            child.wait()
        _atomic_json(work / "openfoam-exit.json", {"phase": phase, "returncode": rc, "stage": stage,
            "process_identity": identity, "finished_at": _now(), "evidence_mode": "REAL"})
    return rc


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Internal fixed OpenFOAM supervisor")
    parser.add_argument("--run-job", type=Path, required=True)
    args = parser.parse_args()
    raise SystemExit(_run_job(args.run_job))


__all__ = ["OpenFoamAdapter", "make_channel_template", "template_sha256", "read_template_metadata"]
