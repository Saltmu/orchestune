"""Contract tests for the pure `status:*` role table and transition plan (#1217)."""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator
from types import MappingProxyType

import pytest

from orchestune.labels import StatusLabel
from orchestune.ledger.status_machine import (
    ACTIVE_LABELS,
    ALLOWED_TRANSITIONS,
    ESCALATION_LABELS,
    LABEL_ROLES,
    LIFECYCLE_ROLES,
    LabelRole,
    TransitionPlan,
    is_allowed,
    lifecycle_labels,
    plan_transition,
)

ACTIVE = (StatusLabel.QUEUED, StatusLabel.BLOCKED, StatusLabel.IN_PROGRESS)
ESCALATION = (StatusLabel.BLOCKED_HUMAN_REVIEW, StatusLabel.MANUAL_MERGE_REQUIRED)
FINAL = (StatusLabel.DONE, StatusLabel.NOT_NEEDED)
AUXILIARY = (
    StatusLabel.BLOCKED_RECOMPUTE,
    StatusLabel.FORCE_SERIAL,
    StatusLabel.EXTERNAL_LOCK,
)
LIFECYCLE = (*ACTIVE, *ESCALATION, *FINAL)


class TestLabelRoles:
    def test_every_status_label_has_exactly_one_declared_role(self) -> None:
        assert set(LABEL_ROLES) == set(StatusLabel)
        assert len(LABEL_ROLES) == len(StatusLabel)

    @pytest.mark.parametrize(
        ("labels", "role"),
        [
            (ACTIVE, LabelRole.ACTIVE),
            (ESCALATION, LabelRole.ESCALATION),
            (FINAL, LabelRole.FINAL),
            (AUXILIARY, LabelRole.AUXILIARY),
        ],
    )
    def test_role_assignment(
        self, labels: tuple[StatusLabel, ...], role: LabelRole
    ) -> None:
        assert {label: LABEL_ROLES[label] for label in labels} == dict.fromkeys(
            labels, role
        )

    def test_role_table_is_read_only(self) -> None:
        assert isinstance(LABEL_ROLES, MappingProxyType)
        with pytest.raises(TypeError):
            LABEL_ROLES[StatusLabel.QUEUED] = LabelRole.FINAL  # type: ignore[index]

    def test_lifecycle_roles_exclude_auxiliary(self) -> None:
        assert LIFECYCLE_ROLES == {
            LabelRole.ACTIVE,
            LabelRole.ESCALATION,
            LabelRole.FINAL,
        }
        assert LabelRole.AUXILIARY not in LIFECYCLE_ROLES

    def test_lifecycle_has_seven_labels_including_final(self) -> None:
        derived = {
            label for label, role in LABEL_ROLES.items() if role in LIFECYCLE_ROLES
        }
        assert derived == set(LIFECYCLE)
        assert len(derived) == 7

    def test_explicit_orders_follow_the_existing_constants(self) -> None:
        assert ACTIVE_LABELS == (
            StatusLabel.IN_PROGRESS,
            StatusLabel.QUEUED,
            StatusLabel.BLOCKED,
        )
        assert ESCALATION_LABELS == (
            StatusLabel.BLOCKED_HUMAN_REVIEW,
            StatusLabel.MANUAL_MERGE_REQUIRED,
        )
        assert {LABEL_ROLES[label] for label in ACTIVE_LABELS} == {LabelRole.ACTIVE}
        assert {LABEL_ROLES[label] for label in ESCALATION_LABELS} == {
            LabelRole.ESCALATION
        }


class TestLifecycleLabels:
    def test_returns_known_lifecycle_labels_as_a_set(self) -> None:
        result = lifecycle_labels(("status:queued", "status:done"))
        assert result == frozenset({StatusLabel.QUEUED, StatusLabel.DONE})
        assert isinstance(result, frozenset)

    def test_covers_every_lifecycle_label(self) -> None:
        assert lifecycle_labels(tuple(LIFECYCLE)) == frozenset(LIFECYCLE)

    def test_ignores_auxiliary_and_unknown_labels(self) -> None:
        labels = (
            *AUXILIARY,
            "status:unknown",
            "ci:base-branch-red",
            "priority:high",
            "",
            "STATUS:QUEUED",
        )
        assert lifecycle_labels(labels) == frozenset()

    def test_mixed_input_keeps_only_lifecycle_labels(self) -> None:
        labels = ("priority:high", "status:blocked", "status:force-serial")
        assert lifecycle_labels(labels) == frozenset({StatusLabel.BLOCKED})

    def test_duplicates_collapse_and_empty_input_is_empty(self) -> None:
        assert lifecycle_labels(["status:queued"] * 3) == {StatusLabel.QUEUED}
        assert lifecycle_labels(()) == frozenset()

    def test_accepts_any_iterable(self) -> None:
        def generate() -> Iterator[str]:
            yield "status:in-progress"
            yield "status:blocked-recompute"

        assert lifecycle_labels(generate()) == {StatusLabel.IN_PROGRESS}


class TestAllowedTransitions:
    def test_is_an_immutable_set_of_label_pairs(self) -> None:
        assert isinstance(ALLOWED_TRANSITIONS, frozenset)
        assert all(
            isinstance(pair, tuple) and len(pair) == 2 for pair in ALLOWED_TRANSITIONS
        )

    def test_only_lifecycle_labels_take_part(self) -> None:
        for source, target in ALLOWED_TRANSITIONS:
            assert LABEL_ROLES[source] in LIFECYCLE_ROLES
            assert LABEL_ROLES[target] in LIFECYCLE_ROLES

    @pytest.mark.parametrize("label", LIFECYCLE)
    def test_every_lifecycle_label_has_an_explicit_self_transition(
        self, label: StatusLabel
    ) -> None:
        assert (label, label) in ALLOWED_TRANSITIONS
        assert is_allowed(label, label)

    @pytest.mark.parametrize("label", AUXILIARY)
    def test_auxiliary_labels_are_not_lifecycle_transitions(
        self, label: StatusLabel
    ) -> None:
        assert not any(label in pair for pair in ALLOWED_TRANSITIONS)
        assert not is_allowed(StatusLabel.BLOCKED, label)
        assert not is_allowed(label, StatusLabel.QUEUED)

    @pytest.mark.parametrize("source", ESCALATION)
    @pytest.mark.parametrize("target", ACTIVE)
    def test_escalation_never_returns_to_active_automatically(
        self, source: StatusLabel, target: StatusLabel
    ) -> None:
        assert not is_allowed(source, target)

    @pytest.mark.parametrize("source", FINAL)
    @pytest.mark.parametrize("target", [*ACTIVE, *ESCALATION])
    def test_final_labels_only_reach_escalation_when_listed(
        self, source: StatusLabel, target: StatusLabel
    ) -> None:
        expected = (source, target) == (
            StatusLabel.NOT_NEEDED,
            StatusLabel.BLOCKED_HUMAN_REVIEW,
        )
        assert is_allowed(source, target) is expected

    @pytest.mark.parametrize(
        ("source", "target"),
        [
            # claim / launch
            (StatusLabel.QUEUED, StatusLabel.IN_PROGRESS),
            (StatusLabel.BLOCKED, StatusLabel.IN_PROGRESS),
            # dependency (un)blocking and requeue
            (StatusLabel.BLOCKED, StatusLabel.QUEUED),
            (StatusLabel.QUEUED, StatusLabel.BLOCKED),
            (StatusLabel.IN_PROGRESS, StatusLabel.QUEUED),
            (StatusLabel.IN_PROGRESS, StatusLabel.BLOCKED),
            # completion and replan
            (StatusLabel.IN_PROGRESS, StatusLabel.DONE),
            (StatusLabel.QUEUED, StatusLabel.DONE),
            (StatusLabel.BLOCKED, StatusLabel.DONE),
            (StatusLabel.QUEUED, StatusLabel.NOT_NEEDED),
            (StatusLabel.BLOCKED, StatusLabel.NOT_NEEDED),
            (StatusLabel.IN_PROGRESS, StatusLabel.NOT_NEEDED),
            # escalation
            (StatusLabel.QUEUED, StatusLabel.BLOCKED_HUMAN_REVIEW),
            (StatusLabel.BLOCKED, StatusLabel.BLOCKED_HUMAN_REVIEW),
            (StatusLabel.IN_PROGRESS, StatusLabel.BLOCKED_HUMAN_REVIEW),
            (StatusLabel.NOT_NEEDED, StatusLabel.BLOCKED_HUMAN_REVIEW),
            (StatusLabel.IN_PROGRESS, StatusLabel.MANUAL_MERGE_REQUIRED),
        ],
    )
    def test_inventoried_normal_transitions_are_allowed(
        self, source: StatusLabel, target: StatusLabel
    ) -> None:
        assert (source, target) in ALLOWED_TRANSITIONS
        assert is_allowed(source, target)

    def test_done_never_leaves_done(self) -> None:
        outgoing = {t for s, t in ALLOWED_TRANSITIONS if s == StatusLabel.DONE}
        assert outgoing == {StatusLabel.DONE}

    def test_is_allowed_accepts_plain_strings_and_rejects_unknown(self) -> None:
        assert is_allowed("status:queued", "status:in-progress")
        assert not is_allowed("status:unknown", "status:queued")
        assert not is_allowed("status:queued", "status:unknown")
        assert not is_allowed("", "")

    def test_cycle_context_non_terminal_table_is_a_subset(self) -> None:
        # dispatch/cycle_context_state keeps its own cycle-local table; it must
        # never allow a transition the status-machine policy forbids.
        from orchestune.dispatch.cycle_context_state import _NON_TERMINAL_TRANSITIONS

        for source, targets in _NON_TERMINAL_TRANSITIONS.items():
            for target in targets:
                assert is_allowed(source, target), (source, target)


class TestTransitionPlan:
    def test_is_an_immutable_value_object(self) -> None:
        plan = TransitionPlan(add="status:done", remove=("status:queued",))
        assert dataclasses.is_dataclass(plan)
        with pytest.raises(dataclasses.FrozenInstanceError):
            plan.add = "status:blocked"  # type: ignore[misc]
        with pytest.raises(dataclasses.FrozenInstanceError):
            plan.remove = ()  # type: ignore[misc]
        assert plan == TransitionPlan("status:done", ("status:queued",))
        assert hash(plan) == hash(TransitionPlan("status:done", ("status:queued",)))

    def test_fields_are_add_then_remove(self) -> None:
        assert [f.name for f in dataclasses.fields(TransitionPlan)] == ["add", "remove"]

    def test_has_no_instance_dict(self) -> None:
        plan = TransitionPlan("a", ())
        assert not hasattr(plan, "__dict__")


class TestPlanTransition:
    def test_returns_add_and_remove_from_the_snapshot(self) -> None:
        plan = plan_transition("status:done", ("status:in-progress",))
        assert plan == TransitionPlan("status:done", ("status:in-progress",))
        assert isinstance(plan.remove, tuple)

    def test_preserves_input_order_without_sorting(self) -> None:
        old = ("status:queued", "status:blocked", "status:in-progress")
        assert plan_transition("status:done", old).remove == old
        reordered = tuple(reversed(old))
        assert plan_transition("status:done", reordered).remove == reordered

    def test_preserves_duplicates(self) -> None:
        old = ("status:queued", "status:queued", "status:blocked", "status:queued")
        assert plan_transition("status:done", old).remove == old

    def test_excludes_only_the_new_label_every_time_it_appears(self) -> None:
        old = ("status:blocked", "status:queued", "status:blocked", "status:done")
        plan = plan_transition("status:blocked", old)
        assert plan.add == "status:blocked"
        assert plan.remove == ("status:queued", "status:done")

    def test_self_label_only_leaves_nothing_to_remove(self) -> None:
        assert plan_transition("status:queued", ("status:queued",)).remove == ()

    def test_empty_old_labels_only_add(self) -> None:
        assert plan_transition("status:in-progress", ()) == TransitionPlan(
            "status:in-progress", ()
        )

    def test_accepts_unknown_strings_without_validation(self) -> None:
        plan = plan_transition("custom:new", ("custom:old", "", "status:queued"))
        assert plan == TransitionPlan("custom:new", ("custom:old", "", "status:queued"))

    def test_does_not_enforce_the_allowed_transition_table(self) -> None:
        # done -> queued is not allowed by policy, yet planning must not refuse it.
        assert not is_allowed(StatusLabel.DONE, StatusLabel.QUEUED)
        plan = plan_transition(StatusLabel.QUEUED, (StatusLabel.DONE,))
        assert plan.remove == (StatusLabel.DONE,)

    def test_does_not_deduplicate_escalation_or_final_labels(self) -> None:
        old = (StatusLabel.DONE, StatusLabel.BLOCKED_HUMAN_REVIEW)
        assert plan_transition(StatusLabel.QUEUED, old).remove == old

    def test_is_deterministic_and_does_not_consume_global_state(self) -> None:
        old = ("status:queued", "status:blocked")
        assert plan_transition("status:done", old) == plan_transition(
            "status:done", old
        )
