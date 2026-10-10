"""Dependency-resolution bounded liveness on production code (#1265, #1219 §4).

Replay: pytest -n0 this file --hypothesis-seed=<seed>. Hypothesis prints the
shrunk initialize/rule sequence; retain it as a deterministic regression.
"""

import os
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import patch

import pytest
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, initialize, rule

from tests.dependency_liveness_test_support import (
    ACTIVE_PATHS,
    BASE_RED,
    CASE_TABLE,
    DEPENDENT,
    ISSUE_BY_SUBTASK,
    LIVENESS_BOUND,
    RECOMPUTE,
    CompletionPath,
    CycleObservation,
    FaultPlan,
    LivenessTopology,
    LivenessWorld,
    OracleView,
    StatusLabel,
    assert_case,
    run_case,
)

CASES = {case.name: case for case in CASE_TABLE}


@pytest.mark.parametrize("case", CASE_TABLE, ids=lambda case: case.name)
def test_each_completion_path_promotes_at_the_case_table_cycle(tmp_path, case):
    assert_case(run_case(tmp_path, case), case)


def test_case_table_matches_the_design_and_explains_every_deviation():
    assert set(CASES) == {
        "label",
        "record_completion",
        "dry_run",
        "dry_run_record_completion",
        "outcome_not_needed",
        "prior_merge",
        "status_repair",
        "recompute_release",
        "multiple_dependencies",
    }
    assert LIVENESS_BOUND == 1
    for case in CASE_TABLE:
        assert case.expected_delay in (0, None)
        assert case.expected_delay is not None or case.reason


def test_not_needed_save_failure_holds_cycle_and_retries(tmp_path):
    world = LivenessWorld(tmp_path)
    world.cycle()
    world.complete(11, CompletionPath.OUTCOME_NOT_NEEDED)

    with patch(
        "orchestune.dispatch.gc.save_run_state",
        side_effect=OSError("disk full"),
    ):
        failed = world.cycle()

    assert isinstance(failed.error, OSError)
    assert not failed.promoted
    assert StatusLabel.NOT_NEEDED.value in world.labels(11)
    assert "11" in world._ledger().active_worktrees
    assert world._ledger().completed_worktrees == []

    retried = world.cycle()
    assert retried.promoted
    assert retried.propagated() == {11}
    assert "11" not in world._ledger().active_worktrees


@pytest.mark.parametrize(
    ("case_name", "faults"),
    [
        ("record_completion", FaultPlan(empty_completion_set=True)),
        ("prior_merge", FaultPlan(empty_completion_set=True)),
        ("recompute_release", FaultPlan(empty_completion_set=True)),
        ("status_repair", FaultPlan(throwaway_repair_context=True)),
    ],
    ids=["round4-record", "round4-prior-merge", "round4-recovery", "round5-context"],
)
def test_injected_miswiring_is_detected(tmp_path, case_name, faults):
    """#902 Round 4/5: an empty completion set or a throwaway context must fail."""
    case = CASES[case_name]
    with pytest.raises(AssertionError):
        assert_case(run_case(tmp_path, case, faults), case)


def test_miswiring_faults_do_not_affect_label_evidence(tmp_path):
    """The control faults target only context-carried evidence."""
    case = CASES["label"]
    faults = FaultPlan(empty_completion_set=True, throwaway_repair_context=True)
    assert_case(run_case(tmp_path, case, faults), case)


@pytest.mark.parametrize("apply", [True, False], ids=["apply", "dry-run"])
def test_base_branch_red_hold_prevents_promotion(tmp_path, apply):
    world = LivenessWorld(tmp_path)
    world.complete(11, CompletionPath.LABEL)
    world.set_t_label(BASE_RED, True)
    for _ in range(LIVENESS_BOUND + 1):
        observation = world.cycle(apply=apply)
        assert not observation.promoted and not observation.previewed


def test_unreleased_completion_reservation_prevents_promotion(tmp_path):
    world = LivenessWorld(tmp_path)
    world.complete(11, CompletionPath.LABEL)
    world.set_reservation(11, True)
    for _ in range(LIVENESS_BOUND + 1):
        observation = world.cycle()
        assert not observation.promoted and not observation.previewed
    world.set_reservation(11, False)
    assert world.cycle().promoted


def test_dry_run_preview_respects_unreleased_completion_reservation(tmp_path):
    world = LivenessWorld(tmp_path)
    world.complete(11, CompletionPath.LABEL)
    world.set_reservation(11, True)
    assert not world.cycle(apply=False).previewed


def test_recompute_release_respects_base_branch_red_hold(tmp_path):
    world = LivenessWorld(tmp_path, t_labels=("status:blocked", RECOMPUTE, BASE_RED))
    world.complete(11, CompletionPath.LABEL)
    assert not world.cycle().promoted


def test_recompute_release_revalidates_dependency_evidence(tmp_path):
    world = LivenessWorld(tmp_path, t_labels=("status:blocked", RECOMPUTE))
    world.complete(11, CompletionPath.LABEL)
    assert not world.cycle(before_promotion=lambda w: w.revoke(11)).promoted


def test_stale_snapshot_hold_added_before_promotion_is_respected(tmp_path):
    world = LivenessWorld(tmp_path)
    world.complete(11, CompletionPath.LABEL)
    observation = world.cycle(before_promotion=lambda w: w.set_t_label(BASE_RED, True))
    assert not observation.promoted


def test_stale_snapshot_revoked_dependency_is_not_promoted(tmp_path):
    world = LivenessWorld(tmp_path)
    world.complete(11, CompletionPath.LABEL)
    observation = world.cycle(before_promotion=lambda w: w.revoke(11))
    assert not observation.promoted


def assert_safe(
    observation: CycleObservation, dependencies: tuple[int, ...] | None
) -> None:
    """No new promotion (or dry-run preview) without fixture-valid evidence.

    Apply re-reads Forge before mutating, so it is judged at the promotion
    point (after stale-snapshot changes).  A dry run never re-reads; its preview
    is defined over the context snapshot and is judged at cycle start. Under
    listing lag, the executor's fresh reads of dependencies return the initial
    context snapshot, so it is judged at cycle start.
    """
    if observation.apply:
        view = (
            replace(
                observation.at_start,
                base_red=observation.at_promotion.base_red,
            )
            if observation.listing_lag
            else observation.at_promotion
        )
    else:
        view = observation.at_start
    if view.promotable(dependencies, apply=observation.apply):
        return
    if not observation.apply:
        reserved_conflict = (
            bool((set(dependencies) | {DEPENDENT}) & view.reserved)
            if dependencies is not None
            else False
        )
        assert (
            reserved_conflict or not observation.previewed
        ), f"unsafe preview: {observation}"
    else:
        assert not observation.promoted, f"unsafe promotion: {observation}"


def _known_intermediate_dry_run_reservation(
    observation: CycleObservation,
    required: tuple[int, ...],
    deps: tuple[str, ...],
    number: int,
) -> bool:
    """DRY_RUN_RESERVATION_ISSUE (#1267) for an intermediate node, as for T.

    Excused only when an unreleased reservation is the sole reason: every
    dependency has preview-visible evidence at cycle start (#1281).
    """
    view = observation.at_start
    return (
        not observation.apply
        and len(required) == len(deps)
        and set(required) <= view.preview_visible
        and bool((set(required) | {number}) & view.reserved)
    )


def assert_intermediate_safety(
    world: LivenessWorld, observation: CycleObservation
) -> None:
    if world.topology is None:
        return
    for number, deps in world.topology.issue_depends_on.items():
        if not deps:
            continue
        required = tuple(ISSUE_BY_SUBTASK[d] for d in deps if d in ISSUE_BY_SUBTASK)
        known_self_reservation = False
        if len(required) < len(deps):
            deps_satisfied = False
        else:
            req_set = set(required)

            def _satisfied(
                view: OracleView,
                req_set: set[int] = req_set,
                number: int = number,
                own_reservation: bool = True,
            ) -> bool:
                evidence = view.valid if observation.apply else view.preview_visible
                return (
                    req_set <= evidence
                    and not (req_set & view.reserved)
                    and not (own_reservation and number in view.reserved)
                )

            view = (
                observation.at_start
                if not observation.apply or observation.listing_lag
                else observation.at_promotion
            )
            deps_satisfied = _satisfied(view)
            # SELF_RESERVATION_PREVIEW_ISSUE (#1283): a dry run previews an issue whose
            # own reservation is unreleased although its dependencies are satisfied.
            known_self_reservation = not observation.apply and _satisfied(
                view, own_reservation=False
            )
        if not deps_satisfied and not known_self_reservation:
            assert (
                number not in observation.promotion_issue_numbers
                or _known_intermediate_dry_run_reservation(
                    observation, required, deps, number
                )
            ), f"unsafe intermediate promotion event for {number}: {observation}"
            if observation.apply:
                before = observation.labels_before.get(number, frozenset())
                after = observation.labels_after.get(number, frozenset())
                if (
                    StatusLabel.BLOCKED.value in before
                    and StatusLabel.QUEUED.value not in before
                ):
                    assert (
                        StatusLabel.QUEUED.value not in after
                    ), f"unsafe live transition of intermediate {number} to queued: {observation}"


@st.composite
def dependency_topologies(draw: st.DrawFn) -> LivenessTopology:
    kind = draw(
        st.sampled_from(
            (
                "simple_one",
                "simple_two",
                "fan_in_three",
                "fan_in_four",
                "transitive_chain",
                "branching_diamond",
                "intermediate_cycle",
                "dependent_cycle",
                "unresolved_direct",
                "unresolved_transitive",
            )
        )
    )
    if kind == "simple_one":
        return LivenessTopology(
            dep_issues=(11,),
            t_depends_on=("dep-a",),
            issue_depends_on={},
            required_to_promote=(11,),
        )
    if kind == "simple_two":
        return LivenessTopology(
            dep_issues=(11, 12),
            t_depends_on=("dep-a", "dep-b"),
            issue_depends_on={},
            required_to_promote=(11, 12),
        )
    if kind == "fan_in_three":
        return LivenessTopology(
            dep_issues=(11, 12, 13),
            t_depends_on=("dep-a", "dep-b", "dep-c"),
            issue_depends_on={},
            required_to_promote=(11, 12, 13),
        )
    if kind == "fan_in_four":
        return LivenessTopology(
            dep_issues=(11, 12, 13, 14),
            t_depends_on=("dep-a", "dep-b", "dep-c", "dep-d"),
            issue_depends_on={},
            required_to_promote=(11, 12, 13, 14),
        )
    if kind == "transitive_chain":
        # T -> 11 -> 12 -> 13
        return LivenessTopology(
            dep_issues=(11, 12, 13),
            t_depends_on=("dep-a",),
            issue_depends_on={11: ("dep-b",), 12: ("dep-c",)},
            required_to_promote=(11,),
        )
    if kind == "branching_diamond":
        # T -> (11, 12); 11 -> 13; 12 -> 13
        return LivenessTopology(
            dep_issues=(11, 12, 13),
            t_depends_on=("dep-a", "dep-b"),
            issue_depends_on={11: ("dep-c",), 12: ("dep-c",)},
            required_to_promote=(11, 12),
        )
    if kind == "intermediate_cycle":
        # 11 -> 12, 12 -> 11; T -> 11
        return LivenessTopology(
            dep_issues=(11, 12),
            t_depends_on=("dep-a",),
            issue_depends_on={11: ("dep-b",), 12: ("dep-a",)},
            required_to_promote=(11,),
        )
    if kind == "dependent_cycle":
        # T -> 11, 11 -> dependent
        return LivenessTopology(
            dep_issues=(11,),
            t_depends_on=("dep-a",),
            issue_depends_on={11: ("dependent",)},
            required_to_promote=(11,),
        )
    if kind == "unresolved_direct":
        # T -> ("dep-a", "unresolved-missing")
        return LivenessTopology(
            dep_issues=(11,),
            t_depends_on=("dep-a", "unresolved-missing"),
            issue_depends_on={},
            required_to_promote=None,
        )
    # unresolved_transitive: T -> 11, 11 -> "unresolved-missing"
    return LivenessTopology(
        dep_issues=(11,),
        t_depends_on=("dep-a",),
        issue_depends_on={11: ("unresolved-missing",)},
        required_to_promote=(11,),
    )


class DependencyLivenessMachine(RuleBasedStateMachine):
    """Random completion, restart, fault, hold and snapshot sequences."""

    def __init__(self):
        super().__init__()
        root = Path(".orchestune/tmp")
        root.mkdir(parents=True, exist_ok=True)
        self.directory = TemporaryDirectory(prefix="liveness-1265-", dir=root)
        self.fair_streak = 0
        self.intermediate_fair_streaks: dict[int, int] = {}
        self.stale: str | None = None

    @initialize(topology=dependency_topologies(), recompute=st.booleans())
    def start(self, topology: LivenessTopology, recompute: bool):
        labels = ("status:blocked", RECOMPUTE) if recompute else ("status:blocked",)
        self.world = LivenessWorld(
            Path(self.directory.name), t_labels=labels, topology=topology
        )
        self.intermediate_fair_streaks = {
            n: 0 for n in topology.issue_depends_on if topology.issue_depends_on[n]
        }

    def _completable(self) -> list[int]:
        return [
            n
            for n in self.world.all_dep_issues
            if n not in self.world.evidence
            and StatusLabel.QUEUED.value in self.world.labels(n)
        ]

    @rule(
        data=st.data(),
        path=st.sampled_from(
            (
                CompletionPath.LABEL,
                CompletionPath.RECORD_COMPLETION,
                CompletionPath.PRIOR_MERGE,
            )
        ),
    )
    def complete_dependency(self, data, path):
        completable = self._completable()
        if completable:
            self.world.complete(data.draw(st.sampled_from(completable)), path)

    @rule(data=st.data())
    def duplicate_completion(self, data):
        if self.world.evidence:
            number = data.draw(st.sampled_from(sorted(self.world.evidence)))
            self.world.duplicate(number)

    @rule(apply=st.booleans(), lag=st.booleans())
    def cycle(self, apply, lag):
        self.world.faults.listing_lag = lag
        stale, self.stale = self.stale, None
        observation = self.world.cycle(
            apply=apply, before_promotion=_STALE_CHANGES.get(stale)
        )
        assert_safe(observation, self.world.required)
        assert_intermediate_safety(self.world, observation)
        # Eligibility at both boundaries determines fairness. A stale callback
        # that changes nothing (or only unrelated state) cannot reset liveness.
        fair = (
            observation.error is None
            and not observation.fault_injected
            and self.world.faults.forge_operation is None
            and all(
                view.promotable(self.world.required, apply=apply)
                for view in (observation.at_start, observation.at_promotion)
            )
        )
        self.fair_streak = self.fair_streak + 1 if fair else 0
        if self.fair_streak >= LIVENESS_BOUND:
            assert "status:queued" in observation.t_after or observation.previewed

        if self.world.topology is not None:
            for number, deps in self.world.topology.issue_depends_on.items():
                if not deps:
                    continue
                before_labels = observation.labels_before.get(number, frozenset())
                if (
                    StatusLabel.BLOCKED.value not in before_labels
                    or StatusLabel.QUEUED.value in before_labels
                ):
                    self.intermediate_fair_streaks[number] = 0
                    continue
                required_for_number = tuple(
                    ISSUE_BY_SUBTASK[d] for d in deps if d in ISSUE_BY_SUBTASK
                )
                if len(required_for_number) < len(deps):
                    self.intermediate_fair_streaks[number] = 0
                    continue
                fair_for_number = (
                    observation.error is None
                    and not observation.fault_injected
                    and self.world.faults.forge_operation is None
                    and all(
                        set(required_for_number)
                        <= (view.valid if apply else view.preview_visible)
                        and not (set(required_for_number) & view.reserved)
                        and number not in view.reserved
                        for view in (observation.at_start, observation.at_promotion)
                    )
                )
                self.intermediate_fair_streaks[number] = (
                    self.intermediate_fair_streaks.get(number, 0) + 1
                    if fair_for_number
                    else 0
                )
                if self.intermediate_fair_streaks[number] >= LIVENESS_BOUND:
                    if apply:
                        after_labels = observation.labels_after.get(number, frozenset())
                        assert (
                            StatusLabel.QUEUED.value in after_labels
                        ), f"intermediate {number} not promoted to queued despite fair streak: {observation}"
                    else:
                        assert (
                            number in observation.promotion_issue_numbers
                        ), f"intermediate {number} not previewed despite fair streak: {observation}"

    @rule(ledger_loss=st.booleans())
    def restart(self, ledger_loss):
        if ledger_loss:
            self.world.lose_ledger()

    @rule(
        op=st.sampled_from(("add", "remove")), mode=st.sampled_from(("before", "after"))
    )
    def fail_next(self, op, mode):
        self.world.faults.forge_operation = (op, mode)

    @rule(
        kind=st.sampled_from(("base_red", "recompute", "reservation")),
        data=st.data(),
    )
    def toggle_hold(self, kind, data):
        if kind == "reservation":
            candidates = (DEPENDENT, *self.world.all_dep_issues)
            target = data.draw(st.sampled_from(candidates))
            present = target in self.world.oracle().reserved
            self.world.set_reservation(target, not present)
        else:
            label = BASE_RED if kind == "base_red" else RECOMPUTE
            self.world.set_t_label(label, label not in self.world.labels())

    @rule(kind=st.sampled_from(("add_hold", "revoke", "complete")))
    def stale_snapshot(self, kind):
        self.stale = kind

    def teardown(self):
        self.directory.cleanup()


def _stale_complete(world: LivenessWorld) -> None:
    completable = [
        n
        for n in world.all_dep_issues
        if n not in world.evidence and StatusLabel.QUEUED.value in world.labels(n)
    ]
    if completable:
        world.complete(completable[0], CompletionPath.LABEL)


def _stale_revoke(world: LivenessWorld) -> None:
    # Active completions and verified prior merges are collected before this
    # callback; the bound context legitimately retains their confirmed receipt.
    collected_paths = ACTIVE_PATHS | {CompletionPath.PRIOR_MERGE}
    revocable = [n for n, path in world.evidence.items() if path not in collected_paths]
    if revocable:
        world.revoke(revocable[0])


_STALE_CHANGES = {
    "add_hold": lambda world: world.set_t_label(BASE_RED, True),
    "revoke": _stale_revoke,
    "complete": _stale_complete,
}


TestDependencyLivenessMachine = DependencyLivenessMachine.TestCase


@pytest.mark.parametrize("intermediate", [False, True])
@pytest.mark.parametrize("kind", ["complete", "revoke"])
@pytest.mark.parametrize("apply", [False, True])
@pytest.mark.parametrize("lag", [False, True])
@pytest.mark.parametrize("suppress_promotion", [False, True])
def test_noop_stale_callback_counts_toward_liveness(
    monkeypatch, intermediate, kind, apply, lag, suppress_promotion
):
    from tests.dependency_liveness_test_support import cycle_module

    topology = LivenessTopology(
        dep_issues=(11, 12) if intermediate else (11,),
        t_depends_on=("dep-a",),
        issue_depends_on={11: ("dep-b",)} if intermediate else {},
        required_to_promote=(11,),
    )
    machine = DependencyLivenessMachine()
    try:
        machine.start(topology=topology, recompute=False)
        dependency = 12 if intermediate else 11
        subject = 11 if intermediate else DEPENDENT
        # Collect a receipt first: evidence is visible to dry-run and cannot be
        # revoked by the stale callback, while complete also has no eligible D.
        machine.world.complete(dependency, CompletionPath.RECORD_COMPLETION)
        machine.world.cycle()
        machine.world.relabel(subject, (StatusLabel.BLOCKED.value,))
        if suppress_promotion:
            real_boundary = cycle_module._run_status_repair_boundary

            def skip_promotion(boundary, finding_code, **kwargs):
                if boundary == "status-blocked-promotion":
                    return []
                return real_boundary(boundary, finding_code, **kwargs)

            monkeypatch.setattr(
                cycle_module, "_run_status_repair_boundary", skip_promotion
            )
        machine.stale_snapshot(kind)
        if suppress_promotion:
            with pytest.raises(AssertionError):
                machine.cycle(apply=apply, lag=lag)
        else:
            machine.cycle(apply=apply, lag=lag)
            streak = (
                machine.intermediate_fair_streaks[subject]
                if intermediate
                else machine.fair_streak
            )
            assert streak >= LIVENESS_BOUND
    finally:
        machine.teardown()


def test_configured_profile_applies_to_liveness_machine():
    applied: Any = TestDependencyLivenessMachine.settings
    profile = os.environ.get("HYPOTHESIS_PROFILE", "ci")
    assert settings.get_current_profile_name() == profile
    if profile == "ci":
        assert applied.deadline is None and applied.print_blob


@pytest.mark.parametrize("intermediate", [False, True])
def test_stale_revoke_preserves_collected_prior_merge_receipt(intermediate):
    machine = DependencyLivenessMachine()
    topology = LivenessTopology(
        dep_issues=(11, 12, 13) if intermediate else (11,),
        t_depends_on=("dep-a",),
        issue_depends_on={11: ("dep-b",), 12: ("dep-c",)} if intermediate else {},
        required_to_promote=(11,),
    )
    try:
        machine.start(topology=topology, recompute=False)
        dependency = 13 if intermediate else 11
        machine.stale_snapshot("revoke")
        machine.world.complete(dependency, CompletionPath.PRIOR_MERGE)
        machine.cycle(apply=True, lag=False)
        observation = machine.world.observations[-1]
        subject = 12 if intermediate else DEPENDENT
        assert subject in observation.promotion_issue_numbers
        assert dependency in observation.at_promotion.valid
    finally:
        machine.teardown()
