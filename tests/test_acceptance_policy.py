"""Pure-policy tests with synthetic metadata, never real solver evidence."""
from __future__ import annotations

import copy

import pytest

from dsh_sim.review.acceptance import run_acceptance_blockers

pytestmark = pytest.mark.mock


def snapshots():
    return (
        [{"run_id": "run-1", "current_attempt_id": "attempt-2",
          "execution_state": "SUCCEEDED", "numerical_state": "PASS",
          "applicability_state": "IN_SCOPE"}],
        [{"artifact_id": "raw-2", "run_id": "run-1", "attempt_id": "attempt-2",
          "state": "COMMITTED", "evidence_mode": "REAL"}],
        [{"verification_id": "check-2", "run_id": "run-1", "attempt_id": "attempt-2",
          "conclusion": "PASS", "source_artifact_ids": ["raw-2"]}],
    )


def test_affirmative_metadata_passes_policy_only():
    assert run_acceptance_blockers(*snapshots()) == {}


@pytest.mark.parametrize("field,bad_values,code", [
    ("execution_state", ["QUEUED", "RUNNING", "FAILED", "LOST", None, "FUTURE"], "execution_not_succeeded"),
    ("numerical_state", ["NOT_CHECKED", "FAIL", "INSUFFICIENT", None, "FUTURE"], "numerical_not_pass"),
    ("applicability_state", ["OUT_OF_SCOPE", "UNCONFIRMED", None, "FUTURE"], "applicability_not_in_scope"),
])
def test_all_nonaffirmative_states_are_blocked(field, bad_values, code):
    for value in bad_values:
        runs, arts, checks = snapshots()
        runs[0][field] = value
        assert run_acceptance_blockers(runs, arts, checks)[code] == ["run-1"]


@pytest.mark.parametrize("mode", ["MOCK", "UNKNOWN", None, "", "real", "FUTURE"])
def test_every_nonreal_mode_is_blocked(mode):
    runs, arts, checks = snapshots()
    arts[0]["evidence_mode"] = mode
    assert run_acceptance_blockers(runs, arts, checks)["evidence_not_real"] == ["raw-2"]


def test_empty_runs_cannot_vacuously_pass():
    assert "runs_missing" in run_acceptance_blockers([], [], [])


@pytest.mark.parametrize("attempt_id", [None, ""])
def test_missing_selected_attempt_is_blocked(attempt_id):
    runs, arts, checks = snapshots()
    runs[0]["current_attempt_id"] = attempt_id
    assert "selected_attempt_missing" in run_acceptance_blockers(runs, arts, checks)


@pytest.mark.parametrize("case", ["absent", "previous_attempt", "other_run", "temporary"])
def test_missing_or_unrelated_committed_evidence_is_blocked(case):
    runs, arts, checks = snapshots()
    if case == "absent":
        arts = []
    elif case == "previous_attempt":
        arts[0]["attempt_id"] = "attempt-1"
    elif case == "other_run":
        arts[0]["run_id"] = "other-run"
    else:
        arts[0]["state"] = "TEMP"
    result = run_acceptance_blockers(runs, arts, checks)
    assert "selected_attempt_evidence_missing" in result
    assert "verification_source_mismatch" in result


@pytest.mark.parametrize("case", ["absent", "previous_attempt", "other_run"])
def test_previous_or_missing_verification_cannot_be_reused(case):
    runs, arts, checks = snapshots()
    if case == "absent":
        checks = []
    elif case == "previous_attempt":
        checks[0]["attempt_id"] = "attempt-1"
    else:
        checks[0]["run_id"] = "other-run"
    assert "selected_attempt_verification_missing" in run_acceptance_blockers(runs, arts, checks)


@pytest.mark.parametrize("conclusion", ["FAIL", "INSUFFICIENT", "NOT_CHECKED", None, "FUTURE"])
def test_run_pass_does_not_override_nonpass_verification(conclusion):
    runs, arts, checks = snapshots()
    checks[0]["conclusion"] = conclusion
    assert "verification_not_pass" in run_acceptance_blockers(runs, arts, checks)


@pytest.mark.parametrize("refs", [[], None, ["not-registered"], ["raw-2", "other-run-art"], "raw-2", [{}]])
def test_verification_requires_current_attempt_sources(refs):
    runs, arts, checks = snapshots()
    checks[0]["source_artifact_ids"] = refs
    assert "verification_source_mismatch" in run_acceptance_blockers(runs, arts, checks)


def test_a_passing_check_does_not_mask_another_failed_check():
    runs, arts, checks = snapshots()
    checks.append({**checks[0], "verification_id": "check-fail", "conclusion": "FAIL"})
    assert run_acceptance_blockers(runs, arts, checks)["verification_not_pass"] == ["check-fail"]


def test_unrelated_history_is_not_selected_as_current_evidence():
    runs, arts, checks = snapshots()
    arts.append({**arts[0], "artifact_id": "old", "attempt_id": "attempt-1", "evidence_mode": "MOCK"})
    checks.append({**checks[0], "verification_id": "old-check", "attempt_id": "attempt-1", "conclusion": "FAIL"})
    assert run_acceptance_blockers(runs, arts, checks) == {}


def test_policy_is_deterministic_and_does_not_mutate_inputs():
    runs, arts, checks = snapshots()
    arts.extend([{**arts[0], "artifact_id": "b", "evidence_mode": None},
                 {**arts[0], "artifact_id": "a", "evidence_mode": "MOCK"}])
    before = copy.deepcopy((runs, arts, checks))
    result = run_acceptance_blockers(runs, arts, checks)
    assert result["evidence_not_real"] == ["a", "b"]
    assert result == run_acceptance_blockers(runs, list(reversed(arts)), checks)
    assert (runs, arts, checks) == before
