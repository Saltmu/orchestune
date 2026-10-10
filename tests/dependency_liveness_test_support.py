"""Production-code harness for dependency-resolution bounded liveness (#1265).

One cycle runs the real ``_prepare_cycle_context`` (which builds the
``CycleContext`` with ``_build_cycle_context``) and the real
``execute_pipeline`` phases on top of one in-memory Forge and the run-state
file.  Only effects unrelated to promotion are stubbed: git/worktree/process
probes and the scheduling phase, which runs after promotion and would otherwise
launch T.  Planner, executor and ``CycleContext`` are never replaced.

The oracle never asks production whether a dependency is complete.  It reads
only fixture data -- raw Forge labels, the evidence this harness injected, and
whether the run-state file still holds the entry an evidence kind needs -- and
the case table below.
"""

from __future__ import annotations

import types
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Any
from unittest.mock import patch

from orchestune.consistency.models import RepairCommand, RepairResult
from orchestune.dependencies.assessment import DependencyAssessment, DependencyState
from orchestune.dispatch import cycle as cycle_module
from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.cycle_actions import CycleActionAdapter
from orchestune.dispatch.phase_scheduling import SchedulingPhaseResult
from orchestune.dispatch.rules import CycleContext
from orchestune.dispatch.status_repair import status_intent_journal_path
from orchestune.labels import StatusLabel
from orchestune.ledger.run_state import RunState, load_run_state
from orchestune.models import IssueRecord, PrRecord
from orchestune.outcome_record import OutcomeRecord
from tests.conftest import FakeForge, make_issue
from tests.dispatch_test_support import (
    GC_PROCESS_ALIVE_TARGETS,
    flat_active_worktree,
    save_locked_run_state,
)
from tests.status_reconciliation_test_support import FaultBoundary
from tests.verification_contract_test_support import require

PARENT = 100
DEPENDENT = 20
SUBTASK_BY_ISSUE = {
    11: "dep-a",
    12: "dep-b",
    13: "dep-c",
    14: "dep-d",
    DEPENDENT: "dependent",
}
ISSUE_BY_SUBTASK = {v: k for k, v in SUBTASK_BY_ISSUE.items()}
PARENT_BRANCH = f"parent/issue-{PARENT}"
BASE_RED = "ci:base-branch-red"
RECOMPUTE = StatusLabel.BLOCKED_RECOMPUTE.value
MERGED_AT = "2026-01-02T00:00:00Z"
REOPENED_AT = "2026-01-03T00:00:00Z"
FINAL_LABELS = frozenset({StatusLabel.DONE.value, StatusLabel.NOT_NEEDED.value})
#: Guaranteed bound N: T is promoted within N cycles of fair evidence (#1219 §4).
LIVENESS_BOUND = 1
#: Counterexamples split out to their own production Issues (footprint stays test).
SELF_RESERVATION_PREVIEW_ISSUE = "#1283"


@dataclass(frozen=True, slots=True)
class LivenessTopology:
    """Dependency graph for stateful exploration (#1265)."""

    dep_issues: tuple[int, ...] = (11,)
    t_depends_on: tuple[str, ...] = ("dep-a",)
    issue_depends_on: dict[int, tuple[str, ...]] = field(default_factory=dict)
    required_to_promote: tuple[int, ...] | None = (11,)


class CompletionPath(StrEnum):
    """How the fixture hands a dependency's completion evidence to production."""

    LABEL = "label"
    RECORD_COMPLETION = "record_completion"
    OUTCOME_NOT_NEEDED = "outcome_not_needed"
    PRIOR_MERGE = "prior_merge"


#: Paths whose evidence lives in the local ledger until the cycle collects it.
ACTIVE_PATHS = frozenset(
    {CompletionPath.RECORD_COMPLETION, CompletionPath.OUTCOME_NOT_NEEDED}
)


@dataclass(frozen=True, slots=True)
class LivenessCase:
    """One completion-path row; delays count from the evidence cycle (0).

    ``expected_delay`` is ``None`` only for a documented reason in ``reason``:
    the observable (label or dry-run ``PromotionEvent``) never shows T.
    """

    name: str
    paths: tuple[CompletionPath, ...]
    apply: bool = True
    expected_delay: int | None = 0
    t_labels: tuple[str, ...] = (StatusLabel.BLOCKED.value,)
    listing_lag: bool = False
    reason: str = ""
    known_bug: str | None = None


CASE_TABLE: tuple[LivenessCase, ...] = (
    LivenessCase("label", (CompletionPath.LABEL,)),
    LivenessCase("record_completion", (CompletionPath.RECORD_COMPLETION,)),
    LivenessCase("dry_run", (CompletionPath.LABEL,), apply=False),
    LivenessCase(
        "dry_run_record_completion",
        (CompletionPath.RECORD_COMPLETION,),
        apply=False,
        expected_delay=None,
        reason=(
            "#882: a same-cycle completion is confirmed only after save_run_state "
            "succeeds, which a dry run never performs; #873 removed the unsaved "
            "completion overlay, so the preview cannot show T"
        ),
    ),
    LivenessCase(
        "outcome_not_needed",
        (CompletionPath.OUTCOME_NOT_NEEDED,),
    ),
    LivenessCase("prior_merge", (CompletionPath.PRIOR_MERGE,)),
    LivenessCase(
        "status_repair",
        (CompletionPath.RECORD_COMPLETION,),
        listing_lag=True,
        reason="the executor's fresh reads still see D in progress (#902 Round 5)",
    ),
    LivenessCase(
        "recompute_release",
        (CompletionPath.RECORD_COMPLETION,),
        t_labels=(StatusLabel.BLOCKED.value, RECOMPUTE),
        reason="reconcile_recovery promotes from the bound context (#902 Round 4)",
    ),
    LivenessCase(
        "multiple_dependencies",
        (CompletionPath.LABEL, CompletionPath.RECORD_COMPLETION),
    ),
)


@dataclass(slots=True)
class FaultPlan:
    """Test-only faults; none of them exists in production code."""

    forge_operation: tuple[str, str] | None = None
    listing_lag: bool = False
    empty_completion_set: bool = False
    throwaway_repair_context: bool = False


@dataclass(frozen=True, slots=True)
class OracleView:
    """Fixture-derived expectation inputs at one point of a cycle."""

    valid: frozenset[int]
    preview_visible: frozenset[int]
    base_red: bool
    reserved: frozenset[int]

    def promotable(
        self,
        dependencies: tuple[int, ...] | None,
        *,
        apply: bool,
        subject: int | None = DEPENDENT,
    ) -> bool:
        if dependencies is None:
            return False
        evidence = self.valid if apply else self.preview_visible
        return (
            set(dependencies) <= evidence
            and not self.base_red
            and not (set(dependencies) & self.reserved)
            and (subject is None or subject not in self.reserved)
        )


@dataclass(slots=True)
class CycleObservation:
    index: int
    apply: bool
    t_before: frozenset[str]
    t_after: frozenset[str]
    promotion_issue_numbers: tuple[int, ...]
    assessment: DependencyAssessment | None
    at_start: OracleView
    at_promotion: OracleView
    error: BaseException | None = None
    fault_injected: bool = False
    listing_lag: bool = False
    labels_before: dict[int, frozenset[str]] = field(default_factory=dict)
    labels_after: dict[int, frozenset[str]] = field(default_factory=dict)

    @property
    def promoted(self) -> bool:
        queued = StatusLabel.QUEUED.value
        return (
            StatusLabel.BLOCKED.value in self.t_before
            and queued not in self.t_before
            and queued in self.t_after
        )

    @property
    def previewed(self) -> bool:
        return DEPENDENT in self.promotion_issue_numbers

    def propagated(self) -> frozenset[int]:
        """Dependencies the real promotion decision received as completed."""
        if self.assessment is None:
            return frozenset()
        return frozenset(
            item.issue_number
            for item in self.assessment.resolved
            if item.state is DependencyState.COMPLETED
        )


class _LivenessForge(FaultBoundary):
    """Phase 2 fault boundary plus optional read lag for dependency Issues.

    While ``frozen`` is set, child listings and dependency reads return the
    context-build snapshot: a dependency's completion has not yet reached the
    executor's fresh re-fetch (#902 Round 5).  T itself is always read live.
    """

    def __init__(self, forge: FakeForge) -> None:
        super().__init__(forge)
        self.frozen: dict[int, IssueRecord] | None = None
        self.fault_injected: bool = False

    def mutate(self, op: str, number: int | str, label: str) -> None:
        # Completion paths may remove the last lifecycle label (outcome-only
        # not-needed closes the Issue), so Phase 2's lifecycle assertion is
        # not reused; the before/after fault points are the same.
        mode = None
        if self.armed is not None and self.armed[0] == op:
            _, mode = self.armed
            self.armed = None
        if mode == "before":
            self.fault_injected = True
            raise OSError("injected before mutation")
        getattr(self.forge, op + "_label")(number, label)
        self.history.append((op, int(number), label))
        if mode == "after":
            self.fault_injected = True
            raise OSError("injected lost response")

    def _lagging(self, number: int | str) -> IssueRecord | None:
        if self.frozen is None or int(number) == DEPENDENT:
            return None
        return self.frozen.get(int(number))

    def get_issue(self, number: int | str) -> IssueRecord | None:
        return self._lagging(number) or self.forge.get_issue(number)

    def get_issue_labels(self, number: int | str) -> tuple[str, ...]:
        lagging = self._lagging(number)
        return lagging.labels if lagging else self.forge.get_issue_labels(number)

    def list_sub_issues(self, parent: int | str) -> list[IssueRecord]:
        return self._children(self.forge.list_sub_issues(parent))

    def find_issues_by_parent_metadata(self, parent: int | str) -> list[IssueRecord]:
        return self._children(self.forge.find_issues_by_parent_metadata(parent))

    def _children(self, live: list[IssueRecord]) -> list[IssueRecord]:
        return [self._lagging(issue.number) or issue for issue in live]


def _issue(number: int, labels: tuple[str, ...], depends_on=()) -> IssueRecord:
    subtask = SUBTASK_BY_ISSUE[number]
    return make_issue(
        number,
        labels=labels,
        subtask_id=subtask,
        depends_on=depends_on,
        footprint=(f"src/{subtask}.py",),
        parent={"number": PARENT},
    )


@dataclass(slots=True)
class LivenessWorld:
    """Parent EPIC, dependencies D and dependent T on one in-memory Forge."""

    root: Path
    dependencies: tuple[int, ...] = (11,)
    t_labels: tuple[str, ...] = (StatusLabel.BLOCKED.value,)
    forge: FakeForge = field(default_factory=FakeForge)
    faults: FaultPlan = field(default_factory=FaultPlan)
    evidence: dict[int, CompletionPath] = field(default_factory=dict)
    evidence_cycle: dict[int, int] = field(default_factory=dict)
    cycle_index: int = 0
    observations: list[CycleObservation] = field(default_factory=list)
    topology: LivenessTopology | None = None
    required: tuple[int, ...] | None = field(init=False)
    all_dep_issues: tuple[int, ...] = field(init=False)
    boundary: _LivenessForge = field(init=False)

    def __post_init__(self) -> None:
        self.boundary = _LivenessForge(self.forge)
        self.forge.seed_issue(make_issue(PARENT, labels=(), parent=None))
        self.forge.branches.add(PARENT_BRANCH)
        if self.topology is not None:
            self.all_dep_issues = self.topology.dep_issues
            self.required = self.topology.required_to_promote
            t_depends_on = self.topology.t_depends_on
            for number in self.all_dep_issues:
                issue_deps = self.topology.issue_depends_on.get(number, ())
                initial_labels = (
                    (StatusLabel.BLOCKED.value,)
                    if issue_deps
                    else (StatusLabel.QUEUED.value,)
                )
                self.forge.seed_issue(
                    _issue(number, initial_labels, depends_on=issue_deps)
                )
        else:
            self.all_dep_issues = self.dependencies
            self.required = self.dependencies
            t_depends_on = tuple(SUBTASK_BY_ISSUE[n] for n in self.dependencies)
            for number in self.dependencies:
                self.forge.seed_issue(_issue(number, (StatusLabel.QUEUED.value,)))
        self.forge.seed_issue(_issue(DEPENDENT, self.t_labels, t_depends_on))
        save_locked_run_state(RunState(), self.run_state_path)

    @property
    def run_state_path(self) -> Path:
        return self.root / "run_state.json"

    def config(self, *, apply: bool) -> DispatcherConfig:
        return DispatcherConfig(
            parent_issue_number=PARENT,
            run_state_path=self.run_state_path,
            worktree_root=self.root / "worktrees",
            log_dir=self.root / "logs",
            events_log_path=self.root / "events.jsonl",
            apply=apply,
            forge=self.boundary,
        )

    def labels(self, number: int = DEPENDENT) -> frozenset[str]:
        return frozenset(str(label) for label in self.forge.get_issue_labels(number))

    def relabel(self, number: int, labels: tuple[str, ...]) -> None:
        self.forge.issues[number] = replace(self.forge.issues[number], labels=labels)

    def _ledger(self) -> RunState:
        return load_run_state(self.run_state_path)

    def _save_ledger(self, state: RunState) -> None:
        save_locked_run_state(state, self.run_state_path)

    # ---- evidence ------------------------------------------------------------

    def complete(self, number: int, path: CompletionPath) -> None:
        """Inject valid evidence for ``number``; it is usable from this cycle."""
        if path is CompletionPath.LABEL:
            self.relabel(number, (StatusLabel.DONE.value,))
        elif path is CompletionPath.PRIOR_MERGE:
            self._seed_prior_merge(number)
        else:
            self._seed_active_completion(number, path)
        self.evidence[number] = path
        self.evidence_cycle[number] = self.cycle_index

    def duplicate(self, number: int) -> None:
        """Deliver the same completion evidence again (retry / double report)."""
        path = self.evidence[number]
        if path is CompletionPath.LABEL:
            self.forge.add_label(number, StatusLabel.DONE.value)
        elif path is CompletionPath.PRIOR_MERGE:
            self._seed_prior_merge(number)
        else:
            self._post_outcome(number, path)

    def revoke(self, number: int) -> None:
        """Reopen a dependency whose evidence no longer lives in the ledger."""
        if str(number) in self._ledger().active_worktrees:
            return
        self.evidence.pop(number, None)
        self.evidence_cycle.pop(number, None)
        self.forge.issue_states[number] = "OPEN"
        current = self.forge.issues[number]
        self.forge.issues[number] = replace(
            current, labels=(StatusLabel.QUEUED.value,), state="OPEN"
        )
        self.forge.set_issue_last_reopened_at(number, REOPENED_AT)

    def _seed_prior_merge(self, number: int) -> None:
        subtask = SUBTASK_BY_ISSUE[number]
        self.forge.set_issue_last_reopened_at(number, None)
        self.forge.seed_pr(
            PrRecord(
                number=500 + number,
                head_ref=f"claude/issue-{number}-{subtask}",
                base_ref=PARENT_BRANCH,
                changed_files=(),
                state="MERGED",
                merged_at=MERGED_AT,
                closed_at=MERGED_AT,
                merge_commit_oid=f"{number:040d}",
                is_cross_repository=False,
            ),
            state="merged",
        )

    def _post_outcome(self, number: int, path: CompletionPath) -> None:
        result = "not-needed" if path is CompletionPath.OUTCOME_NOT_NEEDED else "done"
        self.forge.add_comment(
            number, OutcomeRecord(result=result, issue=number).render()
        )

    def _seed_active_completion(self, number: int, path: CompletionPath) -> None:
        subtask = SUBTASK_BY_ISSUE[number]
        self.relabel(number, (StatusLabel.IN_PROGRESS.value,))
        self._post_outcome(number, path)
        state = self._ledger()
        state.active_worktrees[str(number)] = flat_active_worktree(
            issue_number=number,
            branch=f"claude/issue-{number}-{subtask}",
            worktree_path=str(self.root / "worktrees" / subtask),
            pid=40_000 + number,
            started_at=1_699_999_000.0,
            declared_footprint=(f"src/{subtask}.py",),
        )
        self._save_ledger(state)

    # ---- external changes and restarts ---------------------------------------

    def set_t_label(self, label: str, present: bool) -> None:
        labels = set(self.labels())
        labels.add(label) if present else labels.discard(label)
        self.relabel(DEPENDENT, tuple(sorted(labels)))

    def set_reservation(self, number: int, present: bool) -> None:
        """An unreleased completion reservation (``orchestune complete`` in flight)."""
        state = self._ledger()
        key = f"repo::{number}"
        if present:
            state.completion_reservations[key] = {
                "schema_version": 1,
                "repository_id": "repo",
                "issue_number": number,
                "generation_id": f"generation-{number}",
                "completion_id": f"completion-{number}",
                "stage": "reserved",
            }
        else:
            state.completion_reservations.pop(key, None)
        self._save_ledger(state)

    def lose_ledger(self) -> None:
        """Runner restart that loses the local run-state and intent journal."""
        status_intent_journal_path(self.config(apply=True)).unlink(missing_ok=True)
        self._save_ledger(RunState())

    # ---- oracle ----------------------------------------------------------------

    def oracle(self) -> OracleView:
        ledger = self._ledger()
        reserved = frozenset(
            int(record["issue_number"])
            for record in ledger.completion_reservations.values()
        )
        valid: set[int] = set()
        visible: set[int] = set()
        for number, path in self.evidence.items():
            labels = self.labels(number)
            if labels & FINAL_LABELS and StatusLabel.QUEUED.value not in labels:
                valid.add(number)
                visible.add(number)
            elif path is CompletionPath.PRIOR_MERGE:
                valid.add(number)
                visible.add(number)
            elif path in ACTIVE_PATHS and str(number) in ledger.active_worktrees:
                # Outcome data can trigger collection, but only a persisted
                # active entry or terminal label is completion evidence.
                valid.add(number)
        return OracleView(
            valid=frozenset(valid),
            preview_visible=frozenset(visible),
            base_red=BASE_RED in self.labels(),
            reserved=reserved,
        )

    # ---- one cycle -------------------------------------------------------------

    def cycle(
        self,
        *,
        apply: bool = True,
        before_promotion: Callable[[LivenessWorld], None] | None = None,
    ) -> CycleObservation:
        self.boundary.fault_injected = False
        t_before = self.labels()
        labels_before = {n: self.labels(n) for n in self.all_dep_issues}
        at_start = self.oracle()
        probe = _PromotionProbe(self, before_promotion)
        error: BaseException | None = None
        try:
            events = run_one_cycle(self, apply=apply, probe=probe)
        except OSError as caught:  # an injected Forge fault aborts this cycle only
            events, error = [], caught
        finally:
            self.boundary.armed = None
        observation = CycleObservation(
            index=self.cycle_index,
            apply=apply,
            t_before=t_before,
            t_after=self.labels(),
            promotion_issue_numbers=tuple(e.issue_number for e in events),
            assessment=probe.assessment,
            at_start=at_start,
            at_promotion=probe.at_promotion or at_start,
            error=error,
            fault_injected=self.boundary.fault_injected,
            listing_lag=self.faults.listing_lag,
            labels_before=labels_before,
            labels_after={n: self.labels(n) for n in self.all_dep_issues},
        )
        self.observations.append(observation)
        self.cycle_index += 1
        return observation


@dataclass(slots=True)
class _PromotionProbe:
    """Observe the bound context exactly where the promotion decision starts."""

    world: LivenessWorld
    before_promotion: Callable[[LivenessWorld], None] | None
    snapshot: dict[int, IssueRecord] = field(default_factory=dict)
    assessment: DependencyAssessment | None = None
    at_promotion: OracleView | None = None

    def __call__(self, ctx: CycleContext) -> None:
        if self.world.faults.listing_lag:
            self.world.boundary.frozen = self.snapshot
        if self.before_promotion is not None:
            self.before_promotion(self.world)
        self.at_promotion = self.world.oracle()
        self.assessment = ctx.assess_dependencies(DEPENDENT)


def _skip_scheduling(ctx, lock_result, deviation_events) -> SchedulingPhaseResult:
    del ctx, lock_result, deviation_events
    return SchedulingPhaseResult(selected=[], quota_slots_available=0, decisions=[])


def _pipeline_api(probe: _PromotionProbe) -> types.ModuleType:
    """The real cycle facade; only post-promotion scheduling is stubbed."""
    api = types.ModuleType("dependency_liveness_cycle_api")
    api.__dict__.update(vars(cycle_module))
    real_reconciliation = cycle_module._run_pre_scheduling_reconciliation

    def reconciliation(**kwargs: Any):
        probe(kwargs["ctx"])
        return real_reconciliation(**kwargs)

    api._run_pre_scheduling_reconciliation = reconciliation  # type: ignore[attr-defined]
    api.run_scheduling_phase = _skip_scheduling  # type: ignore[attr-defined]
    return api


@contextmanager
def _environment_stubs() -> Iterator[None]:
    """Stub only git/worktree/process effects that cannot run in a unit test."""
    with ExitStack() as stack:
        for target in GC_PROCESS_ALIVE_TARGETS:
            stack.enter_context(patch(target, return_value=False))
        for target, value in (
            ("orchestune.dispatch.cycle.ensure_parent_branch_ready", None),
            ("orchestune.dispatch.phase_rebase.list_remote_branches", []),
            ("orchestune.dispatch.gc.completion.remove_worktree", None),
            (
                "orchestune.dispatch.gc.completion.worktree_has_uncommitted_changes",
                False,
            ),
            ("orchestune.dispatch.gc.completion.worktree_has_new_commits", True),
        ):
            stack.enter_context(patch(target, return_value=value))
        yield


def run_one_cycle(world: LivenessWorld, *, apply: bool, probe: _PromotionProbe):
    """Build the real context and run the real pipeline through promotion."""
    config = world.config(apply=apply)
    world.boundary.armed = world.faults.forge_operation
    world.faults.forge_operation = None
    lock_path = config.run_state_path.with_suffix(".lock")
    with (
        cycle_module.run_state_lock(lock_path),
        _environment_stubs(),
        _wiring_faults(world.faults),
    ):
        run_state = load_run_state(config.run_state_path)
        now = 1_700_000_000.0 + world.cycle_index
        issues, ctx, recovery, prior = cycle_module._prepare_cycle_context(
            run_state, config, now
        )
        probe.snapshot = {issue.number: issue for issue in issues.all()}
        repair_cycle = cycle_module._RepairCycleState()
        repair_cycle.add_report(recovery)
        try:
            report = cycle_module.execute_pipeline(
                _pipeline_api(probe),
                ctx,
                issues,
                run_state,
                config,
                now,
                repair_cycle,
                prior.events,
            )
        finally:
            world.boundary.frozen = None
    return list(report.promotion_events)


@contextmanager
def _wiring_faults(faults: FaultPlan) -> Iterator[None]:
    """#902 Round 4/5 analogues; they patch only this test's process."""
    with ExitStack() as stack:
        if faults.empty_completion_set:
            stack.enter_context(
                patch.object(
                    CycleContext, "is_completion_confirmed", _no_confirmed_completion
                )
            )
        if faults.throwaway_repair_context:
            stack.enter_context(
                patch.object(cycle_module, "_ContextRepairExecutor", _ThrowawayExecutor)
            )
        yield


def _no_confirmed_completion(self: CycleContext, issue_number: int) -> bool:
    del self, issue_number
    return False


@dataclass(frozen=True, slots=True)
class _ThrowawayExecutor:
    """Mis-wiring: execute repairs through a freshly built, unrelated context."""

    ctx: CycleContext

    def execute(self, command: RepairCommand) -> RepairResult:
        config = self.ctx.config
        run_state = load_run_state(config.run_state_path)
        actions = CycleActionAdapter(run_state, config, 0.0)
        fresh = cycle_module._build_cycle_context(
            cycle_module._fetch_issues(config), run_state, config, actions=actions
        )
        actions.bind_context(fresh)
        return fresh.execute_repair(command)


def run_case(
    root: Path, case: LivenessCase, faults: FaultPlan | None = None
) -> LivenessWorld:
    """Baseline cycle, then each dependency's evidence in its own cycle."""
    dependencies = tuple(11 + index for index in range(len(case.paths)))
    world = LivenessWorld(root, dependencies, case.t_labels)
    world.faults = faults or FaultPlan()
    world.faults.listing_lag = world.faults.listing_lag or case.listing_lag
    world.cycle(apply=case.apply)
    for number, path in zip(dependencies, case.paths, strict=True):
        world.complete(number, path)
        world.cycle(apply=case.apply)
    for _ in range(LIVENESS_BOUND):
        world.cycle(apply=case.apply)
    return world


def assert_case(world: LivenessWorld, case: LivenessCase) -> None:
    """Promotion happens exactly at the case-table cycle, never before."""
    start = max(world.evidence_cycle[n] for n in world.dependencies)
    expected = None if case.expected_delay is None else start + case.expected_delay
    for observation in world.observations:
        shown = observation.previewed if not case.apply else observation.promoted
        if expected is None or observation.index < expected:
            require(
                "P3C-CASE-DELAY",
                not shown,
                f"{case.name} cycle {observation.index}: T promoted early",
            )
            continue
        if observation.index == expected:
            require(
                "P3C-CASE-DELAY",
                observation.propagated() == set(world.dependencies),
                f"{case.name} cycle {observation.index}: evidence did not reach "
                "the decision",
            )
            require(
                "P3C-CASE-DELAY",
                shown,
                f"{case.name} cycle {observation.index}: T was not promoted",
            )
            require(
                "P3C-CASE-DELAY",
                case.apply or observation.t_after == observation.t_before,
                f"{case.name} cycle {observation.index}: dry run changed labels",
            )
