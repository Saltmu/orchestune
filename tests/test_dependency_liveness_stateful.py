"""Dependency-resolution bounded liveness on production code (#1265, #1219 §4).

Replay: pytest -n0 this file --hypothesis-seed=<seed>. Hypothesis prints the
shrunk initialize/rule sequence; retain it as a deterministic regression.
"""

import os
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import pytest
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, initialize, rule

from tests.dependency_liveness_test_support import (
    ACTIVE_PATHS,
    BASE_RED,
    CASE_TABLE,
    DRY_RUN_RESERVATION_ISSUE,
    LIVENESS_BOUND,
    RECOMPUTE,
    RECOMPUTE_RELEASE_ISSUE,
    CompletionPath,
    CycleObservation,
    FaultPlan,
    LivenessCase,
    LivenessWorld,
    assert_case,
    run_case,
)

CASES = {case.name: case for case in CASE_TABLE}


def _case_param(case: LivenessCase) -> Any:
    marks = (
        [pytest.mark.xfail(reason=f"production bug {case.known_bug}", strict=True)]
        if case.known_bug
        else []
    )
    return pytest.param(case, id=case.name, marks=marks)


@pytest.mark.parametrize("case", [_case_param(case) for case in CASE_TABLE])
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


@pytest.mark.xfail(reason=f"production bug {DRY_RUN_RESERVATION_ISSUE}", strict=True)
def test_dry_run_preview_respects_unreleased_completion_reservation(tmp_path):
    world = LivenessWorld(tmp_path)
    world.complete(11, CompletionPath.LABEL)
    world.set_reservation(11, True)
    assert not world.cycle(apply=False).previewed


@pytest.mark.xfail(reason=f"production bug {RECOMPUTE_RELEASE_ISSUE}", strict=True)
def test_recompute_release_respects_base_branch_red_hold(tmp_path):
    world = LivenessWorld(tmp_path, t_labels=("status:blocked", RECOMPUTE, BASE_RED))
    world.complete(11, CompletionPath.LABEL)
    assert not world.cycle().promoted


@pytest.mark.xfail(reason=f"production bug {RECOMPUTE_RELEASE_ISSUE}", strict=True)
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


def _known_recompute_release_bypass(
    observation: CycleObservation, dependencies: tuple[int, ...]
) -> bool:
    """RECOMPUTE_RELEASE_ISSUE (#1268): the recompute release promotes from the context
    snapshot, ignoring a base-red hold and evidence revoked after the snapshot."""
    snapshot = replace(observation.at_start, base_red=False)
    return RECOMPUTE in observation.t_before and snapshot.promotable(
        dependencies, apply=observation.apply
    )


def assert_safe(observation: CycleObservation, dependencies: tuple[int, ...]) -> None:
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
        known_bug = bool(
            set(dependencies) & view.reserved
        ) or _known_recompute_release_bypass(observation, dependencies)
        assert known_bug or not observation.previewed, f"unsafe preview: {observation}"
    else:
        assert not observation.promoted or _known_recompute_release_bypass(
            observation, dependencies
        ), f"unsafe promotion: {observation}"


class DependencyLivenessMachine(RuleBasedStateMachine):
    """Random completion, restart, fault, hold and snapshot sequences."""

    def __init__(self):
        super().__init__()
        root = Path(".orchestune/tmp")
        root.mkdir(parents=True, exist_ok=True)
        self.directory = TemporaryDirectory(prefix="liveness-1265-", dir=root)
        self.fair_streak = 0
        self.disturbed = False
        self.stale: str | None = None

    @initialize(two=st.booleans(), recompute=st.booleans())
    def start(self, two, recompute):
        labels = ("status:blocked", RECOMPUTE) if recompute else ("status:blocked",)
        self.world = LivenessWorld(
            Path(self.directory.name), (11, 12) if two else (11,), labels
        )

    def _pending(self) -> list[int]:
        return [n for n in self.world.dependencies if n not in self.world.evidence]

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
        pending = self._pending()
        if pending:
            self.world.complete(data.draw(st.sampled_from(pending)), path)

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
        assert_safe(observation, self.world.dependencies)
        fair = not (self.disturbed or stale or observation.error) and all(
            view.promotable(self.world.dependencies, apply=apply)
            for view in (observation.at_start, observation.at_promotion)
        )
        if observation.error:
            self.disturbed = True
        elif apply:
            self.disturbed = False
        self.fair_streak = self.fair_streak + 1 if fair else 0
        if self.fair_streak >= LIVENESS_BOUND + 1:
            assert "status:queued" in observation.t_after or observation.previewed

    @rule(ledger_loss=st.booleans())
    def restart(self, ledger_loss):
        if ledger_loss:
            self.world.lose_ledger()
            self.disturbed = True

    @rule(
        op=st.sampled_from(("add", "remove")), mode=st.sampled_from(("before", "after"))
    )
    def fail_next(self, op, mode):
        self.world.faults.forge_operation = (op, mode)
        self.disturbed = True

    @rule(kind=st.sampled_from(("base_red", "recompute", "reservation")))
    def toggle_hold(self, kind):
        if kind == "reservation":
            present = bool(self.world.oracle().reserved)
            self.world.set_reservation(self.world.dependencies[0], not present)
        else:
            label = BASE_RED if kind == "base_red" else RECOMPUTE
            self.world.set_t_label(label, label not in self.world.labels())
        self.disturbed = True

    @rule(kind=st.sampled_from(("add_hold", "revoke", "complete")))
    def stale_snapshot(self, kind):
        self.stale = kind

    def teardown(self):
        self.directory.cleanup()


def _stale_complete(world: LivenessWorld) -> None:
    pending = [n for n in world.dependencies if n not in world.evidence]
    if pending:
        world.complete(pending[0], CompletionPath.LABEL)


def _stale_revoke(world: LivenessWorld) -> None:
    # Exclude collected and confirmed evidence (e.g. RECORD_COMPLETION) from
    # stale revocation, because the cycle context legitimately retains the receipt.
    revocable = [n for n, path in world.evidence.items() if path not in ACTIVE_PATHS]
    if revocable:
        world.revoke(revocable[0])


_STALE_CHANGES = {
    "add_hold": lambda world: world.set_t_label(BASE_RED, True),
    "revoke": _stale_revoke,
    "complete": _stale_complete,
}


TestDependencyLivenessMachine = DependencyLivenessMachine.TestCase


def test_configured_profile_applies_to_liveness_machine():
    applied: Any = TestDependencyLivenessMachine.settings
    profile = os.environ.get("HYPOTHESIS_PROFILE", "ci")
    assert settings.get_current_profile_name() == profile
    if profile == "ci":
        assert applied.deadline is None and applied.print_blob
