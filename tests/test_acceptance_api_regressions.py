"""API regressions; REAL-tagged synthetic gate fixtures, not solver validation.

These reuse the existing review fixture without changing its thresholds or
expected happy path. They must be run with the complete repository environment.
"""
from __future__ import annotations

import pytest

from dsh_sim.db.models import RunRow
from helpers_chain import decide
from test_review_flow import _setup_real_review

pytestmark = [pytest.mark.mock, pytest.mark.integration]


@pytest.mark.parametrize("field,value,code", [
    ("numerical_state", "NOT_CHECKED", "numerical_not_pass"),
    ("applicability_state", "OUT_OF_SCOPE", "applicability_not_in_scope"),
    ("current_attempt_id", "attempt-without-evidence", "selected_attempt_evidence_missing"),
])
def test_accept_rechecks_affirmative_current_attempt_conditions(client, tmp_path, field, value, code):
    ctx = _setup_real_review(client, tmp_path)
    with client.app.state.session_factory() as session:
        run = session.get(RunRow, ctx["run_ids"][0])
        setattr(run, field, value)
        session.commit()
    response = decide(client, ctx["review"], "ACCEPT")
    assert response.status_code == 503, response.text
    assert code in response.json()["details"]


@pytest.mark.parametrize("outcome", ["REQUEST_CHANGES", "REJECT"])
def test_nonaccept_decisions_remain_available_with_incomplete_checks(client, tmp_path, outcome):
    ctx = _setup_real_review(client, tmp_path)
    with client.app.state.session_factory() as session:
        session.get(RunRow, ctx["run_ids"][0]).numerical_state = "NOT_CHECKED"
        session.commit()
    response = decide(client, ctx["review"], outcome)
    assert response.status_code == 201, response.text
    assert response.json()["outcome"] == outcome
