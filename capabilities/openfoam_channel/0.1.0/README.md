# OpenFOAM channel — public validation package

This **DRAFT** method package exercises the existing TaskSpec → preparation →
Run/Attempt → independent verification → frozen evidence pipeline. It does not
authorize an engineering method or replace the STAR buffer-chamber package.

The adapter generates a laminar parallel-plate channel, freezes its dictionaries
and boundary map into a deterministic archive, then runs `blockMesh`, `checkMesh`
and `simpleFoam` in a separate attempt directory. A second geometry and flow
condition exercises transfer without changing the solver implementation.

Actual solver outputs are retained. The incompressible `p` field is kinematic
pressure, so reported pressure in Pa is `rho*p`, with the solver's pressure
reference retained. See the [OpenFOAM pressure documentation](https://doc.openfoam.com/2312/tools/processing/solvers/algorithm-kinematic-pressure/).
Pressure and velocity boundary values are exported with the official
`foamToVTK` utility; they are not filled using an analytical solution.

For a fully developed laminar channel the reference pressure drop is
`12*rho*nu*Umean*L/H^2`. The experiment records the measured pressure drop and
relative difference as diagnostics. Finite-grid and boundary effects remain
visible. No tolerance is prescribed here.

`manifest.json` contains a SHA-256 index of this package's method files.
Changing a rule or definition without updating its manifest blocks resolution;
changing the manifest invalidates TaskSpec's method digest. Engineering
thresholds, scope confirmations and approvals remain `null` / `UNCONFIRMED` /
empty. A successful real solver run therefore still reports numerical
`INSUFFICIENT` and applicability `UNCONFIRMED` until the relevant criteria are
formally frozen.

Run the documented validation entry in `docs/openfoam-validation.md` from an
isolated environment. It must not be used as a production authorization path.
