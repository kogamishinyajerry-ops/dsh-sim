# Public OpenFOAM channel experiment

The adapter implements the existing `StarAdapter` protocol with an explicitly
registered **single-file tar template**. It is limited to a generated planar,
laminar, incompressible Newtonian channel, serial Linux OpenFOAM v1912. It does
not accept arbitrary dictionaries, `Allrun`, solver names, scripts, or commands.
The archived source template is immutable. Only the numerical recipe below and
the named `invalid_div_scheme` test fault can be materialized.

## Template and adapter API

```python
from dsh_sim.adapters.openfoam_adapter import (
    OpenFoamAdapter, make_channel_template, read_template_metadata,
    template_sha256,
)

template = make_channel_template(
    "var/public-templates/channel.tar",
    length=1.0, height=0.1, width=0.01,
    mean_velocity=0.01, nu=0.001, density=1000.0,
    nx=80, ny=20, iterations=1000,
)
adapter = OpenFoamAdapter(
    template_registry={"art_channel": template},
    template_root="var/public-templates",
    allowed_work_root="var/worker",
)
template_digest = template_sha256(template)
boundary_map_digest = read_template_metadata(template)["boundary_map_sha256"]
```

The inlet is a prescribed parabolic profile whose discrete face average equals
`mean_velocity`. `p=0` at the outlet is a gauge reference. The supplied density
is a postprocessing conversion input; the incompressible solver does not solve
a density field. Solver tolerances are numerical algorithm inputs in the
recipe. Engineering acceptance thresholds remain **null/TBD**.

| Whitelist field | Role | Unit | Meaning |
|---|---|---|---|
| `mean_velocity` | `inlet` | `m/s` | Area average of the discrete inlet profile |
| `kinematic_viscosity` | `fluid` | `m2/s` | Newtonian kinematic viscosity |
| `density` | `fluid` | `kg/m3` | Constant used by the recorded SI conversion |

`prepare_case()` writes a separate `prepared.openfoam.tar`. It does not run
iterations. `read_actual_settings()` invokes official `foamDictionary` with
function entries disabled and 17-digit output, then records the observed
settings and parser command/log digests. Launch staging belongs to the Worker:
`<work_root>/<run_id>/attempt-<n>/<prepared artifact filename>`.

For an environment-configured Worker, set all three variables explicitly:
`DSH_SIM_OPENFOAM_TEMPLATE_REGISTRY` (path to JSON `{artifact_id: template_path}`),
`DSH_SIM_OPENFOAM_TEMPLATE_ROOT`, and `DSH_SIM_WORKER_WORK_DIR`. Source the
supported OpenFOAM environment before starting the Python process. No shell
setup script is executed by the adapter.

## Execution and evidence

Fixed argv commands run `blockMesh`, `checkMesh`, `simpleFoam`, then official
`foamToVTK` for the latest boundary fields. The supervisor records launch intent,
actual command argv, executable SHA-256, build output, exit code and process
identity. PID identity includes Linux boot ID, process creation ticks, host and
namespace PIDs, PID namespace and process group. A fresh adapter instance can
poll the durable job. Cancellation requires a verified identity in the same
PID namespace and an observed empty process group; lack of proof stays `LOST`.

The v1912 runtime probed during development exhibited a SHA1 output-stream error
when `surfaceFieldValue` function objects were initialized. This recipe uses
`functions {}` and an independently invoked official exporter. No runtime
library was changed to mask that failure. Other OpenFOAM versions, arbitrary
meshes, MPI, dynamic function objects and remote scheduler execution remain
outside this adapter's verified scope.

Raw evidence includes the source and prepared tar files, mesh addressing,
latest `p/U/phi`, official boundary `.vtp` exports, stdout/stderr logs,
`environment-probe.json`, command/job/process/exit records and
`conversion-evidence.json`. Failed and cancelled runs retain a case archive and
their actual logs; missing boundary samples do not produce substitute results.

`report.csv` uses the existing six-column report contract:

```csv
section,boundary_role,sign_convention,mass_flow_kg_s,total_pressure_pa,static_pressure_pa
```

The conversion reads actual final-time raw `phi` and official exported boundary
`p/U`. It matches exported face centroids against **actual** `polyMesh`
points/faces/boundary addressing, so it does not assume that arrays happen to be
in the same order. The per-face mapping, values, areas and source digests are
recorded in `conversion-evidence.json`.

- Mass flow is `rho * sum(phi)`, retaining the outward-positive sign.
- Static pressure is `rho * areaAverage(p)`.
- Total pressure is `rho * sum(abs(phi)*(p + |U|^2/2)) / sum(abs(phi))`, only when
  real face samples and the one-way-flow weighting are available.
- The density conversion follows OpenFOAM's [kinematic-pressure convention](https://doc.openfoam.com/2312/tools/processing/solvers/algorithm-kinematic-pressure/).

VTK ASCII p/U exports in the probed v1912 build carry approximately six
significant digits and declare Float32. This limits their numerical precision;
raw `phi` retains the solver's 12-digit output. Coordinate matching tolerance is
an explicit export-format matching allowance, never an engineering acceptance
criterion. `monitor.csv` records actual initial residuals, one row per solver
iteration. An exit code of zero has no implication of numerical `PASS`.

## Verification

```bash
PYTHONPATH=src python -m pytest tests/test_openfoam_adapter.py -m mock -q
# Source a supported real runtime first; runs an actual solver, never a fake CLI:
PYTHONPATH=src python -m pytest tests/test_openfoam_adapter.py -m real_solver -q
```

The real tests cover baseline evidence, a changed geometry/flow case, genuine
invalid-scheme solver failure, wall-clock timeout and cancellation after the
solver has begun iterations. Actual test status belongs to the accompanying
execution logs. An unavailable runtime is `NOT_RUN`, not a success. These tests
exercise the adapter; the separate local-validation entrypoint demonstrates
the existing Task/Preparation/Run/Verification/Bundle chain.
