"""状态机全合法/非法迁移矩阵 + Pydantic 输入契约测试（mock 层）。"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from dsh_sim.domain.states import (
    APPLICABILITY_TRANSITIONS,
    EXECUTION_TRANSITIONS,
    NUMERICAL_TRANSITIONS,
    REVIEW_TRANSITIONS,
    TASK_FLOW_TRANSITIONS,
    VALIDITY_TRANSITIONS,
    ApplicabilityState,
    ExecutionState,
    InvalidTransitionError,
    NumericalState,
    ReviewState,
    TaskFlowState,
    ValidityState,
    allowed_transitions,
    can_transition,
    validate_transition,
)
from dsh_sim.domain.schemas import TaskDraft, TaskSpec

from conftest import FAKE_SHA, FAKE_SHA_B, make_spec

pytestmark = pytest.mark.mock

_ALL_MAPS = [
    TASK_FLOW_TRANSITIONS,
    EXECUTION_TRANSITIONS,
    NUMERICAL_TRANSITIONS,
    APPLICABILITY_TRANSITIONS,
    REVIEW_TRANSITIONS,
    VALIDITY_TRANSITIONS,
]


class TestTransitionMatrix:
    """逐维度穷举：映射内迁移必须合法，映射外迁移必须抛 InvalidTransitionError。"""

    @pytest.mark.parametrize("mapping", _ALL_MAPS)
    def test_all_declared_transitions_valid(self, mapping):
        for from_state, targets in mapping.items():
            for to_state in targets:
                validate_transition(from_state, to_state)
                assert can_transition(from_state, to_state)
                assert to_state in allowed_transitions(from_state)

    @pytest.mark.parametrize("mapping", _ALL_MAPS)
    def test_all_undeclared_transitions_invalid(self, mapping):
        for from_state, targets in mapping.items():
            for to_state in type(from_state):
                if to_state not in targets:
                    with pytest.raises(InvalidTransitionError):
                        validate_transition(from_state, to_state)
                    assert not can_transition(from_state, to_state)


class TestKeySemantics:
    """定义书关键语义抽查（不允许的推断）。"""

    def test_task_flow_chain(self):
        chain = [
            TaskFlowState.DRAFT,
            TaskFlowState.PREPARING,
            TaskFlowState.READY,
            TaskFlowState.AUTHORIZED,
            TaskFlowState.ACTIVE,
            TaskFlowState.READY_FOR_REVIEW,
            TaskFlowState.IN_REVIEW,
        ]
        for a, b in zip(chain, chain[1:]):
            validate_transition(a, b)

    def test_task_flow_terminal_states_have_no_exit(self):
        for s in (
            TaskFlowState.ACCEPTED,
            TaskFlowState.CHANGES_REQUESTED,
            TaskFlowState.REJECTED,
        ):
            assert allowed_transitions(s) == frozenset()

    def test_ready_does_not_skip_to_review(self):
        # READY 仅表示准备完成，不能直接进审查
        assert not can_transition(TaskFlowState.READY, TaskFlowState.READY_FOR_REVIEW)

    def test_succeeded_is_terminal_no_transition_to_pass(self):
        # 程序成功 ≠ 数值 PASS：执行维度没有通往数值维度的迁移
        assert allowed_transitions(ExecutionState.SUCCEEDED) == frozenset()

    def test_lost_frozen_only_manual_failed_exit(self):
        # LOST 冻结重派：唯一出口是人工核实后裁定 FAILED
        assert allowed_transitions(ExecutionState.LOST) == frozenset({ExecutionState.FAILED})

    def test_numerical_pass_not_rewritable(self):
        # 旧数值不原位改写
        assert allowed_transitions(NumericalState.PASS) == frozenset()
        assert allowed_transitions(NumericalState.FAIL) == frozenset()
        assert allowed_transitions(NumericalState.INSUFFICIENT) == frozenset()

    def test_applicability_unconfirmed_initial(self):
        validate_transition(ApplicabilityState.UNCONFIRMED, ApplicabilityState.IN_SCOPE)
        validate_transition(ApplicabilityState.UNCONFIRMED, ApplicabilityState.OUT_OF_SCOPE)

    def test_review_accepted_immutable(self):
        assert allowed_transitions(ReviewState.ACCEPTED) == frozenset()
        assert allowed_transitions(ReviewState.REJECTED) == frozenset()

    def test_validity_one_way(self):
        validate_transition(ValidityState.CURRENT, ValidityState.STALE)
        assert not can_transition(ValidityState.STALE, ValidityState.CURRENT)

    def test_cross_dimension_rejected(self):
        with pytest.raises(InvalidTransitionError):
            validate_transition(ExecutionState.QUEUED, NumericalState.PASS)


class TestSpecSchema:
    def test_valid_spec_accepted(self):
        spec = TaskSpec.model_validate(make_spec())
        assert len(spec.variants) == 2

    def test_duplicate_variant_id_rejected(self):
        spec = make_spec()
        spec["variants"][1]["variant_id"] = "A"
        with pytest.raises(ValidationError, match="variant_id"):
            TaskSpec.model_validate(spec)

    def test_duplicate_condition_id_rejected(self):
        spec = make_spec()
        spec["conditions"].append(dict(spec["conditions"][0]))
        with pytest.raises(ValidationError, match="condition_id"):
            TaskSpec.model_validate(spec)

    def test_gauge_pressure_requires_reference(self):
        spec = make_spec()
        q = spec["conditions"][0]["fields"][0]["quantity"]
        q["pressure_kind"] = "gauge"
        q.pop("reference_absolute_pressure", None)
        with pytest.raises(ValidationError):
            TaskSpec.model_validate(spec)

    def test_gauge_pressure_with_reference_accepted(self):
        spec = make_spec()
        q = spec["conditions"][0]["fields"][0]["quantity"]
        q["pressure_kind"] = "gauge"
        q["reference_absolute_pressure"] = {
            "si_value": 101325.0,
            "unit": "Pa",
            "physical_meaning": "reference_atmosphere",
            "source_ref": "site-standard-001",
        }
        TaskSpec.model_validate(spec)

    def test_draft_allows_missing_sections_with_open_questions(self):
        draft = TaskDraft.model_validate(
            {
                "purpose": "design_screening",
                "open_questions": [
                    {"field": "conditions", "responsible": "eng_zhang", "question": "缺工况表"}
                ],
            }
        )
        assert draft.conditions is None
        assert draft.open_questions[0].responsible == "eng_zhang"

    def test_budget_concurrent_bounds(self):
        spec = make_spec()
        spec["execution_budget"]["max_concurrent"] = 3
        with pytest.raises(ValidationError):
            TaskSpec.model_validate(spec)
