"""Fail-closed run/attempt evidence gates for human ACCEPT (FR-19..23).

Pure policy over service-created snapshots; no solver, DB, thresholds or authority.
This is an additional guard, not a replacement for frozen-bundle integrity,
capability release validation, or the human-only decision checks.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


def run_acceptance_blockers(
    runs: Sequence[Mapping[str, Any]],
    artifacts: Sequence[Mapping[str, Any]],
    verifications: Sequence[Mapping[str, Any]],
) -> dict[str, list[str]]:
    """Require affirmative success and REAL evidence for each selected attempt.

    An old attempt's artifacts or PASS verification cannot establish the current
    attempt's readiness. All selected verifications must PASS and reference
    nonempty, committed, same-attempt evidence. Unknown/future states fail closed.
    """
    problems: dict[str, list[str]] = {}

    def block(code: str, object_id: str) -> None:
        problems.setdefault(code, []).append(object_id)

    if not runs:
        return {"runs_missing": ["No runs available for acceptance"]}

    for run in runs:
        run_id = str(run["run_id"])
        for field, required, code in (
            ("execution_state", "SUCCEEDED", "execution_not_succeeded"),
            ("numerical_state", "PASS", "numerical_not_pass"),
            ("applicability_state", "IN_SCOPE", "applicability_not_in_scope"),
        ):
            if run.get(field) != required:
                block(code, run_id)

        attempt_id = run.get("current_attempt_id")
        if not attempt_id:
            block("selected_attempt_missing", run_id)
            continue

        selected_artifacts = [
            art for art in artifacts
            if art.get("run_id") == run_id
            and art.get("attempt_id") == attempt_id
            and art.get("state") == "COMMITTED"
        ]
        source_ids = {art["artifact_id"] for art in selected_artifacts}
        if not selected_artifacts:
            block("selected_attempt_evidence_missing", run_id)
        for art in selected_artifacts:
            if art.get("evidence_mode") != "REAL":
                block("evidence_not_real", str(art["artifact_id"]))

        selected_checks = [
            check for check in verifications
            if check.get("run_id") == run_id and check.get("attempt_id") == attempt_id
        ]
        if not selected_checks:
            block("selected_attempt_verification_missing", run_id)
        for check in selected_checks:
            check_id = str(check["verification_id"])
            if check.get("conclusion") != "PASS":
                block("verification_not_pass", check_id)
            refs = check.get("source_artifact_ids")
            if (
                not isinstance(refs, (list, tuple))
                or not refs
                or any(not isinstance(ref, str) or ref not in source_ids for ref in refs)
            ):
                block("verification_source_mismatch", check_id)

    # Stable diagnostics independent of database row order.
    return {code: sorted(set(ids)) for code, ids in sorted(problems.items())}
