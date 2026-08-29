"""Pure invariants over Issue status labels and the workflow state behind them.

Status divergence is only visible from a repository-wide view: whether
`status:in-progress` is backed by a running agent lives in run state, whether
`status:blocked` should still hold lives in the dependency graph, and whether a
task is forced serial lives in dispatch policy.  Each of those is checked here
against one immutable observation, with no Forge, Git, or process access.

Three rules shape the findings below.

1. **Cardinality first.** A task holds exactly one primary status.  The
   dependency and evidence invariants only run once that is true, so a repair
   plan can never hold two commands that fight over one Issue.
2. **Uncertainty is not divergence.** A fact that is unknown, stale, ambiguous,
   or absent replaces every other finding for its subject with
   `STATUS_OBSERVATION_UNKNOWN`, which no planner may repair.
3. **A declared transition is not a defect.** A live `TransitionIntent` naming
   this task's status label either justifies the intermediate state outright
   (in-progress before its execution is recorded) or keeps the finding visible
   while removing its repairability, so a half-applied multi-step transition is
   never "corrected" mid-flight.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from orchestune.consistency.contracts import Invariant
from orchestune.consistency.intents import intent_is_live
from orchestune.consistency.models import (
    ConsistencyFinding,
    ConsistencyScope,
    DesiredFact,
    DesiredRepositoryState,
    Evidence,
    FactValue,
    FindingSeverity,
    Observation,
    ObservationCertainty,
    ObservedRepositoryState,
    Repairability,
    ScopedObservations,
)
from orchestune.consistency.observation import (
    EXECUTION_KIND_CLOUD,
    EXECUTION_KIND_LOCAL,
    FACT_EXECUTION_KIND,
    FACT_FORGE_REACHABLE,
    FACT_ISSUE_LABELS,
    FACT_ISSUE_STATE,
    FACT_ISSUE_STATUS_LABELS,
)

# The mutually exclusive lifecycle positions of `docs/ja/status-labels.md`.
# `orchestune.dispatch.labels.PRIMARY_STATUS_LABELS` is a deliberately narrower
# tuple — the labels one `transition_status_label` call sweeps away — while this
# is the full set a task must hold exactly one of.
PRIMARY_STATUS_LABELS = (
    "status:blocked",
    "status:blocked-human-review",
    "status:done",
    "status:in-progress",
    "status:manual-merge-required",
    "status:not-needed",
    "status:queued",
)

# Statuses that record a human gate.  Automation may add a label beside one of
# these, but must never remove one to settle a conflict on its own.
TERMINAL_ESCALATION_LABELS = (
    "status:blocked-human-review",
    "status:manual-merge-required",
)

# Labels that intentionally hold a `status:blocked` task back even once its
# dependencies resolve (`dispatch.reconciliation._decide_blocked_promotions`).
PROMOTION_HOLD_LABELS = ("ci:base-branch-red", "status:blocked-recompute")

FORCE_SERIAL_LABEL = "status:force-serial"

# Desired-state fact names produced by `consistency.desired`.
DESIRED_DEPENDENCIES_RESOLVED = "task.dependencies_resolved"
DESIRED_FORCED_SERIAL_ACTIVE = "dispatch.forced_serial_active"
DESIRED_RUN_STATE_ACTIVE = "task.run_state_active"
DESIRED_STATUS_LABEL = "task.status_label"
DESIRED_UNRESOLVED_DEPENDENCIES = "task.unresolved_dependencies"

# Stable finding codes.  These values are persisted in reports and repair
# allowlists, so changing one is a compatibility change.
BLOCKED_PROMOTION_HELD = "status.blocked-promotion-held"
BLOCKED_WITH_RESOLVED_DEPENDENCIES = "status.blocked-with-resolved-dependencies"
DONE_WITH_ACTIVE_EXECUTION = "status.done-with-active-execution"
FORCED_SERIAL_MISMATCH = "status.forced-serial-mismatch"
FORGE_OBSERVATION_UNKNOWN = "status.forge-observation-unknown"
IN_PROGRESS_WITHOUT_EXECUTION = "status.in-progress-without-execution"
PRIMARY_STATUS_CONFLICT = "status.primary-status-conflict"
PRIMARY_STATUS_MISSING = "status.primary-status-missing"
QUEUED_WITH_UNRESOLVED_DEPENDENCIES = "status.queued-with-unresolved-dependencies"
STATUS_OBSERVATION_UNKNOWN = "status.observation-unknown"

# Stable invariant codes.  `plan_status_repairs` requires the repository policy
# by name: it is the only invariant that looks at the Forge reading, so a report
# that never ran it carries no evidence the Forge was answering at all.
REPOSITORY_POLICY_INVARIANT = "status.repository-policy"
TASK_POLICY_INVARIANT = "status.task-policy"

_KNOWN = ObservationCertainty.KNOWN
_ACTIVE_KINDS = frozenset({EXECUTION_KIND_CLOUD, EXECUTION_KIND_LOCAL})


# ---------------------------------------------------------------------------
# Reading one snapshot
# ---------------------------------------------------------------------------


def _fact(scope: ScopedObservations, name: str) -> Observation | None:
    """One uniquely named fact; a duplicated name is not a usable reading."""
    matches = tuple(fact for fact in scope.facts if fact.name == name)
    return matches[0] if len(matches) == 1 else None


type _Predicate = Callable[[FactValue], bool]


def _text(value: FactValue) -> str | None:
    return value if isinstance(value, str) else None


def _label_tuple(value: FactValue) -> tuple[str, ...] | None:
    """Parse a label fact, or `None` when the reading is not a list of labels."""
    if not isinstance(value, tuple) or any(not isinstance(item, str) for item in value):
        return None
    return tuple(item for item in value if isinstance(item, str))


def _is_text(value: FactValue) -> bool:
    return isinstance(value, str)


def _is_optional_text(value: FactValue) -> bool:
    """`None` is a real answer here: a complete snapshot holds no such Issue."""
    return value is None or isinstance(value, str)


def _is_label_tuple(value: FactValue) -> bool:
    return _label_tuple(value) is not None


def _uncertainty(fact: Observation | None, usable: _Predicate) -> str | None:
    """Why a reading cannot be relied on, or `None` when it can.

    A fact whose shape the policy cannot parse is treated exactly like an
    unknown one.  Reading it as "no labels" would turn a malformed payload into
    a confident absence, which is the one conclusion an observation must never
    invent.
    """
    if fact is None:
        return "absent"
    if fact.certainty is not _KNOWN:
        return fact.certainty.value
    return None if usable(fact.value) else "unusable"


def _desired_fact(
    desired: DesiredRepositoryState,
    name: str,
    *,
    scope: ConsistencyScope,
    subject_id: str | None = None,
) -> DesiredFact | None:
    matches = tuple(
        fact
        for fact in desired.facts
        if fact.scope is scope and fact.subject_id == subject_id and fact.name == name
    )
    return matches[0] if len(matches) == 1 else None


def _scopes(
    observed: ObservedRepositoryState, scope: ConsistencyScope
) -> tuple[ScopedObservations, ...]:
    return tuple(entry for entry in observed.observations if entry.scope is scope)


def _tasks(
    observed: ObservedRepositoryState,
) -> tuple[tuple[str, ScopedObservations], ...]:
    """Identified task scopes, ordered so a report reads the same every time."""
    return tuple(
        sorted(
            (
                (entry.subject_id, entry)
                for entry in _scopes(observed, ConsistencyScope.TASK)
                if entry.subject_id is not None
            ),
            key=lambda item: item[0],
        )
    )


def _observation_details(fact: Observation) -> tuple[str, ...]:
    return (
        f"fact={fact.name}",
        f"source={fact.source}",
        f"certainty={fact.certainty.value}",
        *fact.diagnostics,
    )


# ---------------------------------------------------------------------------
# Transition intents
# ---------------------------------------------------------------------------


def _covers_status(fact: DesiredFact, subject_id: str) -> bool:
    """Whether one expected change declares *this task's* status to be moving.

    The scope is part of the match, not a formality: the journal accepts an
    expected change at any scope, and a parent- or repository-scoped fact that
    happens to share the name and subject would otherwise suppress this task's
    findings until the intent expired.
    """
    return (
        fact.name == DESIRED_STATUS_LABEL
        and fact.scope is ConsistencyScope.TASK
        and fact.subject_id == subject_id
    )


def _live_status_intents(
    desired: DesiredRepositoryState,
    subject_id: str,
    observed: ObservedRepositoryState,
) -> tuple[str, ...]:
    """Intent IDs that declare this task's status label to be mid-transition.

    Liveness is re-checked against the observation time rather than trusted from
    the caller, so a settled or expired journal entry cannot keep suppressing a
    repair.  A malformed timestamp is treated as not live for the same reason.
    """
    live: list[str] = []
    for intent in desired.transition_intents:
        if intent.scope is not ConsistencyScope.TASK:
            continue
        if intent.subject_id != subject_id:
            continue
        if not any(
            _covers_status(change, subject_id) for change in intent.expected_changes
        ):
            continue
        try:
            if intent_is_live(intent, now=observed.observed_at):
                live.append(intent.intent_id)
        except (TypeError, ValueError):
            continue
    return tuple(sorted(live))


# ---------------------------------------------------------------------------
# One task's resolved inputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _TaskView:
    """Everything the task-scope invariants need, already read and certain."""

    subject_id: str
    source: Observation
    labels: tuple[str, ...]
    status_labels: tuple[str, ...]
    primary: tuple[str, ...]
    active: bool
    intent_ids: tuple[str, ...]
    desired_status: str | None
    dependencies_resolved: bool | None
    unresolved: tuple[str, ...]

    @property
    def in_transition(self) -> bool:
        return bool(self.intent_ids)


def primary_status_labels(labels: tuple[str, ...]) -> tuple[str, ...]:
    """The primary statuses among `labels`, in a stable order."""
    return tuple(label for label in PRIMARY_STATUS_LABELS if label in labels)


def _desired_status(desired: DesiredRepositoryState, subject_id: str) -> str | None:
    fact = _desired_fact(
        desired,
        DESIRED_STATUS_LABEL,
        scope=ConsistencyScope.TASK,
        subject_id=subject_id,
    )
    if fact is None or fact.value not in PRIMARY_STATUS_LABELS:
        return None
    return str(fact.value)


def _desired_dependencies(
    desired: DesiredRepositoryState, subject_id: str
) -> tuple[bool | None, tuple[str, ...]]:
    resolved = _desired_fact(
        desired,
        DESIRED_DEPENDENCIES_RESOLVED,
        scope=ConsistencyScope.TASK,
        subject_id=subject_id,
    )
    unresolved = _desired_fact(
        desired,
        DESIRED_UNRESOLVED_DEPENDENCIES,
        scope=ConsistencyScope.TASK,
        subject_id=subject_id,
    )
    if resolved is None or not isinstance(resolved.value, bool):
        return None, ()
    names = () if unresolved is None else _strings_value(unresolved.value)
    return resolved.value, names


def _strings_value(value: FactValue) -> tuple[str, ...]:
    if not isinstance(value, tuple):
        return ()
    return tuple(item for item in value if isinstance(item, str))


def _resolve_task(
    task: ScopedObservations,
    subject_id: str,
    observed: ObservedRepositoryState,
    desired: DesiredRepositoryState,
) -> tuple[_TaskView | None, tuple[ConsistencyFinding, ...]]:
    """Resolve one task into a view, into uncertainty findings, or into neither.

    "Neither" is a closed Issue, or one the snapshot confidently reports as
    absent: there are no labels to hold a status policy to.
    """
    kind = _fact(task, FACT_EXECUTION_KIND)
    labels = _fact(task, FACT_ISSUE_LABELS)
    state = _fact(task, FACT_ISSUE_STATE)
    source = _fact(task, FACT_ISSUE_STATUS_LABELS)
    unknown = tuple(
        _unknown_finding(subject_id, name, fact, reason)
        for name, fact, usable in (
            (FACT_EXECUTION_KIND, kind, _is_text),
            (FACT_ISSUE_LABELS, labels, _is_label_tuple),
            (FACT_ISSUE_STATE, state, _is_optional_text),
            (FACT_ISSUE_STATUS_LABELS, source, _is_label_tuple),
        )
        if (reason := _uncertainty(fact, usable)) is not None
    )
    if unknown or kind is None or labels is None or state is None or source is None:
        return None, unknown
    if (_text(state.value) or "").upper() != "OPEN":
        return None, ()
    return _open_task_view(subject_id, kind, labels, source, observed, desired), ()


def _open_task_view(
    subject_id: str,
    kind: Observation,
    labels: Observation,
    source: Observation,
    observed: ObservedRepositoryState,
    desired: DesiredRepositoryState,
) -> _TaskView:
    status_labels = _label_tuple(source.value) or ()
    resolved, unresolved = _desired_dependencies(desired, subject_id)
    return _TaskView(
        subject_id=subject_id,
        source=source,
        labels=_label_tuple(labels.value) or (),
        status_labels=status_labels,
        primary=primary_status_labels(status_labels),
        active=kind.value in _ACTIVE_KINDS,
        intent_ids=_live_status_intents(desired, subject_id, observed),
        desired_status=_desired_status(desired, subject_id),
        dependencies_resolved=resolved,
        unresolved=unresolved,
    )


# ---------------------------------------------------------------------------
# Findings
# ---------------------------------------------------------------------------


def _uncertain_finding(
    code: str,
    subject: str,
    name: str,
    fact: Observation | None,
    reason: str,
    *,
    scope: ConsistencyScope = ConsistencyScope.TASK,
    subject_id: str | None = None,
) -> ConsistencyFinding:
    details = (
        (f"fact={name}", f"certainty={reason}")
        if fact is None
        else (*_observation_details(fact), f"usability={reason}")
    )
    return ConsistencyFinding(
        code=code,
        scope=scope,
        subject_id=subject_id,
        severity=FindingSeverity.WARNING,
        expected=Evidence(
            f"the {subject} observation is current, unambiguous, and readable",
            (f"fact={name}",),
        ),
        observed=Evidence(
            f"the {subject} observation is {reason}",
            details,
            None if fact is None else fact.value,
        ),
        repairability=Repairability.NONE,
    )


def _unknown_finding(
    subject_id: str, name: str, fact: Observation | None, reason: str
) -> ConsistencyFinding:
    return _uncertain_finding(
        STATUS_OBSERVATION_UNKNOWN,
        "status",
        name,
        fact,
        reason,
        subject_id=subject_id,
    )


def _task_finding(
    view: _TaskView,
    code: str,
    *,
    expected: FactValue,
    expected_summary: str,
    observed_summary: str,
    repairability: Repairability,
    severity: FindingSeverity = FindingSeverity.ERROR,
    details: tuple[str, ...] = (),
) -> ConsistencyFinding:
    return ConsistencyFinding(
        code=code,
        scope=ConsistencyScope.TASK,
        subject_id=view.subject_id,
        severity=severity,
        expected=Evidence(expected_summary, value=expected),
        observed=Evidence(
            observed_summary,
            (*_observation_details(view.source), *details),
            view.status_labels,
        ),
        repairability=repairability,
    )


def _guard(
    view: _TaskView, repairability: Repairability
) -> tuple[Repairability, tuple[str, ...]]:
    """Keep a divergence visible while a declared transition is still in flight."""
    if not view.in_transition:
        return repairability, ()
    return Repairability.NONE, (
        f"a live transition intent covers this status: {', '.join(view.intent_ids)}",
    )


def _missing_findings(view: _TaskView) -> tuple[ConsistencyFinding, ...]:
    repairability, details = _guard(
        view,
        Repairability.AUTOMATIC
        if view.desired_status is not None
        else Repairability.MANUAL,
    )
    return (
        _task_finding(
            view,
            PRIMARY_STATUS_MISSING,
            expected=view.desired_status,
            expected_summary="the task holds exactly one primary status label",
            observed_summary="the task holds no primary status label",
            repairability=repairability,
            details=details,
        ),
    )


def _removable(view: _TaskView) -> tuple[str, ...]:
    return tuple(label for label in view.primary if label != view.desired_status)


def _conflict_findings(view: _TaskView) -> tuple[ConsistencyFinding, ...]:
    removable = _removable(view)
    gated = tuple(label for label in removable if label in TERMINAL_ESCALATION_LABELS)
    repairable = view.desired_status in view.primary and not gated
    repairability, details = _guard(
        view, Repairability.AUTOMATIC if repairable else Repairability.MANUAL
    )
    if gated:
        details = (*details, f"human gate would be removed: {', '.join(gated)}")
    return (
        _task_finding(
            view,
            PRIMARY_STATUS_CONFLICT,
            expected=view.desired_status,
            expected_summary="the task holds exactly one primary status label",
            observed_summary="the task holds several primary status labels",
            repairability=repairability,
            details=(*details, f"conflicting labels: {', '.join(view.primary)}"),
        ),
    )


def _cardinality_findings(view: _TaskView) -> tuple[ConsistencyFinding, ...]:
    if len(view.primary) == 1:
        return ()
    return _missing_findings(view) if not view.primary else _conflict_findings(view)


def _in_progress_findings(view: _TaskView) -> tuple[ConsistencyFinding, ...]:
    if view.primary != ("status:in-progress",) or view.active or view.in_transition:
        return ()
    return (
        _task_finding(
            view,
            IN_PROGRESS_WITHOUT_EXECUTION,
            expected=True,
            expected_summary=(
                "status:in-progress is backed by an active execution or a live intent"
            ),
            observed_summary=(
                "status:in-progress has neither an active execution nor a live intent"
            ),
            repairability=Repairability.MANUAL,
        ),
    )


def _done_findings(view: _TaskView) -> tuple[ConsistencyFinding, ...]:
    if view.primary != ("status:done",) or not view.active or view.in_transition:
        return ()
    return (
        _task_finding(
            view,
            DONE_WITH_ACTIVE_EXECUTION,
            expected=False,
            expected_summary="a completed task records no active execution",
            observed_summary="status:done coexists with an active execution",
            repairability=Repairability.MANUAL,
        ),
    )


def _promotion_findings(view: _TaskView) -> tuple[ConsistencyFinding, ...]:
    holds = tuple(label for label in PROMOTION_HOLD_LABELS if label in view.labels)
    if holds:
        return (
            _task_finding(
                view,
                BLOCKED_PROMOTION_HELD,
                expected="status:blocked",
                expected_summary="promotion stays held while a hold label is present",
                observed_summary=(
                    "dependencies are resolved but promotion is intentionally held"
                ),
                repairability=Repairability.NONE,
                severity=FindingSeverity.INFO,
                details=(f"hold labels: {', '.join(holds)}",),
            ),
        )
    repairability, details = _guard(view, Repairability.AUTOMATIC)
    return (
        _task_finding(
            view,
            BLOCKED_WITH_RESOLVED_DEPENDENCIES,
            expected="status:queued",
            expected_summary="a task whose dependencies resolved is queued",
            observed_summary="status:blocked while every dependency is resolved",
            repairability=repairability,
            details=details,
        ),
    )


def _demotion_findings(view: _TaskView) -> tuple[ConsistencyFinding, ...]:
    repairability, details = _guard(view, Repairability.AUTOMATIC)
    return (
        _task_finding(
            view,
            QUEUED_WITH_UNRESOLVED_DEPENDENCIES,
            expected="status:blocked",
            expected_summary="a task with unresolved dependencies stays blocked",
            observed_summary="status:queued while a dependency is unresolved",
            repairability=repairability,
            details=(
                f"unresolved dependencies: {', '.join(view.unresolved) or '(unnamed)'}",
                *details,
            ),
        ),
    )


def _dependency_findings(view: _TaskView) -> tuple[ConsistencyFinding, ...]:
    if view.dependencies_resolved is None:
        return ()
    if view.primary == ("status:blocked",) and view.dependencies_resolved:
        return _promotion_findings(view)
    if view.primary == ("status:queued",) and not view.dependencies_resolved:
        return _demotion_findings(view)
    return ()


def _one_task_findings(
    subject_id: str,
    task: ScopedObservations,
    observed: ObservedRepositoryState,
    desired: DesiredRepositoryState,
) -> tuple[ConsistencyFinding, ...]:
    view, uncertain = _resolve_task(task, subject_id, observed, desired)
    if uncertain:
        return uncertain
    if view is None:
        return ()
    cardinality = _cardinality_findings(view)
    if len(view.primary) != 1:
        return cardinality
    return (
        *_in_progress_findings(view),
        *_done_findings(view),
        *_dependency_findings(view),
    )


def _task_findings(
    observed: ObservedRepositoryState, desired: DesiredRepositoryState
) -> tuple[ConsistencyFinding, ...]:
    return tuple(
        finding
        for subject_id, task in _tasks(observed)
        for finding in _one_task_findings(subject_id, task, observed, desired)
    )


# ---------------------------------------------------------------------------
# Repository scope
# ---------------------------------------------------------------------------


def _is_reachable(value: FactValue) -> bool:
    return value is True


def _forge_findings(
    observed: ObservedRepositoryState,
) -> tuple[ConsistencyFinding, ...]:
    """Report anything short of one certain "the Forge answered" reading.

    A missing, duplicated, stale, or negative reading all leave the same
    question open — whether the labels in this snapshot came from a Forge that
    was actually answering — so all of them must reach `plan_status_repairs`,
    whose global stop is what keeps a blind scan from rewriting labels.
    """
    scopes = _scopes(observed, ConsistencyScope.REPOSITORY)
    fact = _fact(scopes[0], FACT_FORGE_REACHABLE) if len(scopes) == 1 else None
    reason = _uncertainty(fact, _is_reachable)
    if reason is None:
        return ()
    return (
        _uncertain_finding(
            FORGE_OBSERVATION_UNKNOWN,
            "Forge",
            FACT_FORGE_REACHABLE,
            fact,
            reason,
            scope=ConsistencyScope.REPOSITORY,
        ),
    )


def _forced_serial_owners(
    observed: ObservedRepositoryState, desired: DesiredRepositoryState
) -> tuple[tuple[str, ...], bool]:
    """Active subjects carrying `status:force-serial`, and whether that is certain."""
    owners: list[str] = []
    conclusive = True
    for subject_id, task in _tasks(observed):
        active = _desired_fact(
            desired,
            DESIRED_RUN_STATE_ACTIVE,
            scope=ConsistencyScope.TASK,
            subject_id=subject_id,
        )
        if active is None or active.value is not True:
            continue
        labels = _fact(task, FACT_ISSUE_STATUS_LABELS)
        if labels is None or _uncertainty(labels, _is_label_tuple) is not None:
            conclusive = False
            continue
        if FORCE_SERIAL_LABEL in (_label_tuple(labels.value) or ()):
            owners.append(subject_id)
    return tuple(sorted(owners)), conclusive or bool(owners)


def _forced_serial_findings(
    observed: ObservedRepositoryState, desired: DesiredRepositoryState
) -> tuple[ConsistencyFinding, ...]:
    expected = _desired_fact(
        desired, DESIRED_FORCED_SERIAL_ACTIVE, scope=ConsistencyScope.REPOSITORY
    )
    if expected is None or not isinstance(expected.value, bool):
        return ()
    owners, conclusive = _forced_serial_owners(observed, desired)
    if not conclusive or bool(owners) == expected.value:
        return ()
    return (
        ConsistencyFinding(
            code=FORCED_SERIAL_MISMATCH,
            scope=ConsistencyScope.REPOSITORY,
            severity=FindingSeverity.ERROR,
            expected=Evidence(
                "forced-serial dispatch state matches the observed labels",
                (f"{DESIRED_FORCED_SERIAL_ACTIVE}={expected.value}", expected.reason),
                expected.value,
            ),
            observed=Evidence(
                "forced-serial labels disagree with the dispatch state",
                tuple(f"{FORCE_SERIAL_LABEL} owner: {owner}" for owner in owners)
                or (f"no active task carries {FORCE_SERIAL_LABEL}",),
                owners,
            ),
            repairability=Repairability.MANUAL,
        ),
    )


def _repository_findings(
    observed: ObservedRepositoryState, desired: DesiredRepositoryState
) -> tuple[ConsistencyFinding, ...]:
    return (
        *_forge_findings(observed),
        *_forced_serial_findings(observed, desired),
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


type _Evaluator = Callable[
    [ObservedRepositoryState, DesiredRepositoryState],
    tuple[ConsistencyFinding, ...],
]


@dataclass(frozen=True, slots=True)
class _StatusInvariant(Invariant):
    code: str
    scope: ConsistencyScope
    evaluator: _Evaluator

    def evaluate(
        self,
        observed: ObservedRepositoryState,
        desired: DesiredRepositoryState,
    ) -> tuple[ConsistencyFinding, ...]:
        return self.evaluator(observed, desired)


def status_invariants() -> tuple[Invariant, ...]:
    """Return the pure status invariants for repository and task scope."""
    return (
        _StatusInvariant(
            REPOSITORY_POLICY_INVARIANT,
            ConsistencyScope.REPOSITORY,
            _repository_findings,
        ),
        _StatusInvariant(
            TASK_POLICY_INVARIANT,
            ConsistencyScope.TASK,
            _task_findings,
        ),
    )


__all__ = [
    "BLOCKED_PROMOTION_HELD",
    "BLOCKED_WITH_RESOLVED_DEPENDENCIES",
    "DONE_WITH_ACTIVE_EXECUTION",
    "FORCED_SERIAL_MISMATCH",
    "FORCE_SERIAL_LABEL",
    "FORGE_OBSERVATION_UNKNOWN",
    "IN_PROGRESS_WITHOUT_EXECUTION",
    "PRIMARY_STATUS_CONFLICT",
    "PRIMARY_STATUS_LABELS",
    "PRIMARY_STATUS_MISSING",
    "PROMOTION_HOLD_LABELS",
    "QUEUED_WITH_UNRESOLVED_DEPENDENCIES",
    "REPOSITORY_POLICY_INVARIANT",
    "STATUS_OBSERVATION_UNKNOWN",
    "TASK_POLICY_INVARIANT",
    "TERMINAL_ESCALATION_LABELS",
    "primary_status_labels",
    "status_invariants",
]
