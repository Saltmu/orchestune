"""Shared data types, index building, and reading helpers for consistency observations."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from orchestune.consistency.models import (
    ConsistencyScope,
    FactValue,
    Observation,
    ObservationCertainty,
    ScopedObservations,
)
from orchestune.issue_parsing import effective_parent_number
from orchestune.labels import STATUS_LABEL_PREFIX
from orchestune.models import IssueRecord, PrRecord

# ---------------------------------------------------------------------------
# Provenance sources
# ---------------------------------------------------------------------------

SOURCE_COLLECTOR = "collector"
SOURCE_EXTERNAL = "external-execution"
SOURCE_FORGE = "forge"
SOURCE_GIT = "git"
SOURCE_PROCESS = "process"
SOURCE_RUN_STATE = "run-state"
SOURCE_WORKTREE = "worktree"

_KNOWN = ObservationCertainty.KNOWN
_STALE = ObservationCertainty.STALE
_UNKNOWN = ObservationCertainty.UNKNOWN

# The observed fields of an Issue, named as a diagnostic reports them.  Sorted
# by name, so a conflict reads the same whatever order the records arrived in.
FIELD_LABELS = "labels"
FIELD_PARENT = "parent"
FIELD_PARENT_STATE = "parent state"
FIELD_STATE = "state"


# ---------------------------------------------------------------------------
# Inputs and intermediate readings
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ForgeSnapshot:
    """Forge data already fetched by the caller, reused instead of re-queried.

    `fetched_at` is what makes freshness observable: it is when these records
    were true, not when they were read here.  Leaving it `None` means the
    caller makes no freshness claim, and the snapshot is taken at face value.

    The two completeness flags both default to `False`, because the snapshot a
    cycle already has is filtered in both directions and reading a filter as an
    absence is how a repair comes to act on state that is really there:

    - `pull_requests_complete` — `CycleContext.prs` comes from
      `list_open_prs()`, so a task whose pull request has since merged or
      closed simply has no candidate.
    - `issues_complete` — `IssuesByStatus.all()` covers six status labels and
      may be narrowed to one parent, so a parent's children in the snapshot are
      a subset (a `status:blocked-human-review` child is not in it at all).

    Only a caller that fetched every state may claim completeness.
    """

    issues: tuple[IssueRecord, ...] = ()
    pull_requests: tuple[PrRecord, ...] = ()
    fetched_at: datetime | None = None
    pull_requests_complete: bool = False
    issues_complete: bool = False


@dataclass(frozen=True, slots=True)
class ExecutionRecord:
    """One in-flight execution as the dispatcher recorded it in run state.

    Deliberately not `ledger.run_state.ActiveWorktree`: the consistency kernel
    stays independent of dispatch, and callers map their own record onto this
    minimal shape.
    """

    issue_number: int
    branch: str
    worktree_path: str | None = None
    pid: int | None = None
    external_id: str | None = None
    started_at: float | None = None
    kind: str | None = None
    owner_kind: str | None = None
    claim_id: str | None = None
    claim_stage: str | None = None
    launch_phase: str | None = None


@dataclass(frozen=True, slots=True)
class _Reading:
    """One resolved fact before it is named and given provenance."""

    value: FactValue = None
    certainty: ObservationCertainty = _KNOWN
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _Identifier:
    """A resolved `str` identifier (branch, worktree path) and its certainty."""

    value: str | None
    certainty: ObservationCertainty = _KNOWN
    diagnostics: tuple[str, ...] = ()

    def as_reading(self) -> _Reading:
        return _Reading(self.value, self.certainty, self.diagnostics)


@dataclass(frozen=True, slots=True)
class _ForgeReading:
    """The Forge snapshot in use, with how certain and how fresh it is."""

    snapshot: ForgeSnapshot | None
    certainty: ObservationCertainty
    diagnostics: tuple[str, ...]
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class _IssueView:
    """Every version of one Issue the snapshot holds, and where they disagree.

    `IssuesByStatus.all()` concatenates six separately fetched label lists, so
    an Issue that transitions between those requests appears twice with
    different labels or state.  Keeping only one of them would publish an
    arbitrary version as fact and, worse, make the snapshot depend on the order
    the records arrived in.
    """

    records: tuple[IssueRecord, ...]
    conflicts: tuple[str, ...] = ()

    def agrees_on(self, field: str) -> bool:
        return field not in self.conflicts


@dataclass(frozen=True, slots=True)
class _Index:
    """Deterministic lookups over the reused snapshot and run-state records."""

    issues: dict[int, _IssueView]
    pull_requests_by_branch: dict[str, tuple[PrRecord, ...]]
    pull_request_count: int
    executions: dict[int, tuple[ExecutionRecord, ...]]
    declared_branches: dict[int, str]
    task_numbers: tuple[int, ...]
    children_by_parent: dict[int, tuple[int, ...]]
    declared_parent_states: dict[int, tuple[str, ...]]


# ---------------------------------------------------------------------------
# Output and observation emitters
# ---------------------------------------------------------------------------


def _describe(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"


def _read_probe[ProbeT](
    label: str,
    probe: ProbeT | None,
    call: Callable[[ProbeT], FactValue],
) -> _Reading:
    """Run one probe, turning "not configured" and "raised" into unknown.

    A probe that cannot answer leaves the fact unknown with the reason
    attached.  It must never fall through to a confident negative, which a
    repair would read as licence to delete state that is actually there.
    """
    if probe is None:
        return _Reading(None, _UNKNOWN, (f"{label} probe is not configured",))
    try:
        return _Reading(call(probe), _KNOWN)
    except Exception as exc:  # noqa: BLE001 - boundary: probe failures become unknown facts with diagnostics
        return _Reading(None, _UNKNOWN, (f"{label} probe failed: {_describe(exc)}",))


def _existence_reading[ProbeT](
    identifier: _Identifier,
    label: str,
    probe: ProbeT | None,
    call: Callable[[ProbeT, str], FactValue],
) -> _Reading:
    """Probe for the existence of `identifier`, or inherit why it is unknown."""
    if identifier.value is None:
        return identifier.as_reading()
    name = identifier.value
    return _read_probe(label, probe, lambda resolved: call(resolved, name))


Emit = Callable[[str, _Reading], Observation]


def _emitter(source: str, observed_at: datetime) -> Emit:
    """Bind provenance once, so each fact below is named and read in one line."""

    def emit(name: str, reading: _Reading) -> Observation:
        return Observation(
            name=name,
            certainty=reading.certainty,
            source=source,
            observed_at=observed_at,
            value=reading.value,
            diagnostics=reading.diagnostics,
        )

    return emit


def _fact_name(observation: Observation) -> str:
    return observation.name


def _unknown_forge_reading(
    reading: _ForgeReading,
    diagnostics: tuple[str, ...],
    value: FactValue = None,
) -> _Reading:
    """Keep freshness evidence when another Forge uncertainty takes priority."""
    return _Reading(
        value,
        _UNKNOWN,
        (*reading.diagnostics, *diagnostics),
    )


def _scope(
    scope: ConsistencyScope, subject_id: str | None, facts: list[Observation]
) -> ScopedObservations:
    """Freeze one scope's facts into a name-ordered, immutable group."""
    return ScopedObservations(
        scope=scope,
        subject_id=subject_id,
        facts=tuple(sorted(facts, key=_fact_name)),
    )


def forge_reading(reading: _ForgeReading, value: FactValue) -> _Reading:
    """Grade a value taken from the snapshot with the snapshot's certainty."""
    if reading.snapshot is None:
        return _Reading(None, _UNKNOWN, reading.diagnostics)
    return _Reading(value, reading.certainty, reading.diagnostics)


# ---------------------------------------------------------------------------
# Index builders
# ---------------------------------------------------------------------------


def _parent_number(issue: IssueRecord) -> int | None:
    """The parent Issue number, by the same rule the rest of Orchestune uses."""
    number = effective_parent_number(issue)
    if isinstance(number, bool) or not isinstance(number, int):
        return None
    return number


def _parent_state(issue: IssueRecord) -> str | None:
    parent = issue.parent
    state = parent.get("state") if parent else None
    return state if isinstance(state, str) else None


def _issue_labels(issue: IssueRecord) -> FactValue:
    return tuple(sorted(set(issue.labels)))


def _issue_state(issue: IssueRecord) -> FactValue:
    return issue.state


def _issue_status_labels(issue: IssueRecord) -> FactValue:
    return tuple(
        label
        for label in sorted(set(issue.labels))
        if label.startswith(STATUS_LABEL_PREFIX)
    )


_ISSUE_FIELDS: tuple[tuple[str, Callable[[IssueRecord], FactValue]], ...] = (
    (FIELD_LABELS, _issue_labels),
    (FIELD_PARENT, _parent_number),
    (FIELD_PARENT_STATE, _parent_state),
    (FIELD_STATE, _issue_state),
)


def _distinct[RecordT](records: Sequence[RecordT]) -> tuple[RecordT, ...]:
    """Drop exact repeats without changing the first-seen deterministic value."""
    unique: list[RecordT] = []
    for record in records:
        if all(record != kept for kept in unique):
            unique.append(record)
    return tuple(unique)


def _index_pull_requests(
    snapshot: ForgeSnapshot | None,
) -> tuple[dict[str, tuple[PrRecord, ...]], int]:
    grouped: dict[str, list[PrRecord]] = {}
    if snapshot is None:
        return {}, 0
    pull_requests = _distinct(snapshot.pull_requests)
    for pull_request in pull_requests:
        grouped.setdefault(pull_request.head_ref, []).append(pull_request)
    return (
        {
            head_ref: tuple(sorted(records, key=lambda record: record.number))
            for head_ref, records in grouped.items()
        },
        len(pull_requests),
    )


def _index_issues(snapshot: ForgeSnapshot | None) -> dict[int, _IssueView]:
    grouped: dict[int, list[IssueRecord]] = {}
    for issue in snapshot.issues if snapshot else ():
        grouped.setdefault(issue.number, []).append(issue)
    views: dict[int, _IssueView] = {}
    for number, records in grouped.items():
        unique = _distinct(records)
        conflicts = tuple(
            name
            for name, value_of in _ISSUE_FIELDS
            if len({value_of(record) for record in unique}) > 1
        )
        views[number] = _IssueView(unique, conflicts)
    return views


def _index_parents(
    issues: dict[int, _IssueView],
) -> tuple[dict[int, tuple[int, ...]], dict[int, tuple[str, ...]]]:
    """Group observed Issues under their parents, keeping every declared state."""
    children: dict[int, list[int]] = {}
    states: dict[int, set[str]] = {}
    for number in sorted(issues):
        view = issues[number]
        if not view.agrees_on(FIELD_PARENT):
            continue
        parent = _parent_number(view.records[0])
        if parent is None:
            continue
        children.setdefault(parent, []).append(number)
        for record in view.records:
            state = _parent_state(record)
            if state is not None:
                states.setdefault(parent, set()).add(state)
    return (
        {parent: tuple(numbers) for parent, numbers in children.items()},
        {parent: tuple(sorted(declared)) for parent, declared in states.items()},
    )


def _build_index(
    snapshot: ForgeSnapshot | None,
    executions: Sequence[ExecutionRecord],
    branches_by_issue: Mapping[int, str] | None,
) -> _Index:
    issues = _index_issues(snapshot)
    grouped_executions: dict[int, list[ExecutionRecord]] = {}
    for record in executions:
        grouped_executions.setdefault(record.issue_number, []).append(record)
    declared_branches = dict(branches_by_issue or {})
    pull_requests_by_branch, pull_request_count = _index_pull_requests(snapshot)
    children_by_parent, declared_parent_states = _index_parents(issues)
    child_task_numbers = {
        number
        for number, view in issues.items()
        if any(_parent_number(record) is not None for record in view.records)
    }
    task_numbers = (
        (set(issues) - set(children_by_parent))
        | child_task_numbers
        | set(grouped_executions)
        | set(declared_branches)
    )
    return _Index(
        issues=issues,
        pull_requests_by_branch=pull_requests_by_branch,
        pull_request_count=pull_request_count,
        executions={
            number: tuple(sorted(records, key=lambda record: record.branch))
            for number, records in grouped_executions.items()
        },
        declared_branches=declared_branches,
        task_numbers=tuple(sorted(task_numbers)),
        children_by_parent=children_by_parent,
        declared_parent_states=declared_parent_states,
    )
