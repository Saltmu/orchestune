"""Normalized, side-effect-free repository observation.

`ObservationCollector` folds the sources of truth Orchestune already keeps —
Forge Issues and pull requests, dispatcher run state, Git branches, worktree
directories, local processes, and external (cloud) executions — into one
immutable `ObservedRepositoryState` snapshot.

Three rules shape everything below.

1. **Reuse before re-fetching.** A cycle has usually fetched its Issues and
   pull requests already (`IssuesByStatus`, `CycleContext.prs`); handing that
   `ForgeSnapshot` to `collect()` means the Forge probe is never called, so a
   consistency scan costs no extra GitHub API requests.
2. **Absence of knowledge is never knowledge of absence.** A probe that is not
   configured, that raises, or a subject missing from a label-filtered snapshot
   all yield `ObservationCertainty.UNKNOWN` carrying the reason — never a
   confident "it is not there", which would make a repair delete live state.
3. **Observation only.** Every contact with the outside world goes through the
   injected probes, whose Protocols declare read operations exclusively, so
   collecting cannot repair anything.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable

from orchestune.consistency.models import (
    ConsistencyScope,
    FactValue,
    Observation,
    ObservationCertainty,
    ObservedRepositoryState,
    ScopedObservations,
)
from orchestune.issue_parsing import effective_parent_number
from orchestune.models import IssueRecord, PrRecord

# Provenance recorded on every observation, so a finding can name where the
# fact came from without re-deriving it.
SOURCE_COLLECTOR = "observation-collector"
SOURCE_EXTERNAL = "external-execution"
SOURCE_FORGE = "forge"
SOURCE_GIT = "git"
SOURCE_PROCESS = "process"
SOURCE_RUN_STATE = "run-state"
SOURCE_WORKTREE = "worktree"

# Repository-scope fact names.
FACT_EXECUTION_COUNT = "execution_count"
FACT_FORGE_REACHABLE = "forge_reachable"
FACT_ISSUE_COUNT = "issue_count"
FACT_PULL_REQUEST_COUNT = "pull_request_count"

# Parent-scope fact names.
FACT_CHILD_ISSUE_NUMBERS = "child_issue_numbers"
FACT_PARENT_STATE = "parent_state"

# Task-scope fact names.
FACT_BRANCH_EXISTS = "branch_exists"
FACT_BRANCH_NAME = "branch_name"
FACT_EXECUTION_EXTERNAL_ID = "execution_external_id"
FACT_EXECUTION_EXTERNAL_STATUS = "execution_external_status"
FACT_EXECUTION_KIND = "execution_kind"
FACT_EXECUTION_PID = "execution_pid"
FACT_EXECUTION_PROCESS_ALIVE = "execution_process_alive"
FACT_ISSUE_LABELS = "issue_labels"
FACT_ISSUE_NUMBER = "issue_number"
FACT_ISSUE_STATE = "issue_state"
FACT_ISSUE_STATUS_LABELS = "issue_status_labels"
FACT_PULL_REQUEST_BASE_REF = "pull_request_base_ref"
FACT_PULL_REQUEST_HEAD_REF = "pull_request_head_ref"
FACT_PULL_REQUEST_NUMBER = "pull_request_number"
FACT_PULL_REQUEST_STATE = "pull_request_state"
FACT_WORKTREE_EXISTS = "worktree_exists"
FACT_WORKTREE_PATH = "worktree_path"

# `FACT_PARENT_ISSUE_NUMBER` names the parent both at parent scope (its own
# identity) and at task scope (the child's link to it).
FACT_PARENT_ISSUE_NUMBER = "parent_issue_number"

# Values of `FACT_EXECUTION_KIND`.  `EXECUTION_KIND_NONE` is a known absence
# (run state records nothing in flight); `EXECUTION_KIND_UNKNOWN` is a record
# that names no handle at all, which is not the same thing.
EXECUTION_KIND_CLOUD = "cloud"
EXECUTION_KIND_LOCAL = "local"
EXECUTION_KIND_NONE = "none"
EXECUTION_KIND_UNKNOWN = "unknown"

STATUS_LABEL_PREFIX = "status:"

_KNOWN = ObservationCertainty.KNOWN
_STALE = ObservationCertainty.STALE
_UNKNOWN = ObservationCertainty.UNKNOWN

_NO_EXECUTION_HANDLE = (
    "execution records neither a local pid nor an external execution id"
)
_NO_LOCAL_PID = "execution records no local pid"
_NO_LOCAL_PROCESS = "cloud execution has no local process"
_NO_WORKTREE_PATH = "execution records no worktree path"
_NO_BRANCH = "no branch is recorded for this task"
_FILTERED_PULL_REQUESTS = (
    "the reused pull request snapshot is filtered, so finding no match does "
    "not prove that no pull request exists"
)
_FILTERED_CHILDREN = (
    "the reused Issue snapshot is filtered, so these are the children observed "
    "in it, not necessarily every child"
)
_FILTERED_ISSUE_COUNT = (
    "the reused Issue snapshot is filtered, so the observed issue count is not "
    "a complete repository count"
)
_FILTERED_PULL_REQUEST_COUNT = (
    "the reused pull request snapshot is filtered, so the observed pull request "
    "count is not a complete repository count"
)


# ---------------------------------------------------------------------------
# Inputs
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

    Deliberately not `dispatch.state.ActiveWorktree`: the consistency kernel
    stays independent of dispatch, and callers map their own record onto this
    minimal shape.
    """

    issue_number: int
    branch: str
    worktree_path: str | None = None
    pid: int | None = None
    external_id: str | None = None


# ---------------------------------------------------------------------------
# Probes — read-only boundaries, each independently injectable
# ---------------------------------------------------------------------------


@runtime_checkable
class ForgeProbe(Protocol):
    """Fetches Issues and pull requests when no snapshot can be reused."""

    def fetch_snapshot(self) -> ForgeSnapshot: ...


@runtime_checkable
class GitProbe(Protocol):
    """Reports whether a branch resolves locally or on the remote."""

    def branch_exists(self, branch: str) -> bool: ...


@runtime_checkable
class WorktreeProbe(Protocol):
    """Reports whether a worktree directory is present on the filesystem."""

    def worktree_exists(self, path: str) -> bool: ...


@runtime_checkable
class ProcessProbe(Protocol):
    """Reports whether a recorded local pid is still running."""

    def is_alive(self, pid: int) -> bool: ...


@runtime_checkable
class ExternalExecutionProbe(Protocol):
    """Reports the status of a cloud execution, which has no local pid."""

    def status(self, external_id: str) -> str: ...


Clock = Callable[[], datetime]


def _default_clock() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Internal readings
# ---------------------------------------------------------------------------


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
class _ExecutionView:
    """The single execution a task maps to, or why that mapping is ambiguous."""

    record: ExecutionRecord | None = None
    ambiguity: tuple[str, ...] = ()

    @property
    def ambiguous(self) -> bool:
        return bool(self.ambiguity)


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
    except Exception as exc:  # noqa: BLE001 - every failure becomes an unknown
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


# ---------------------------------------------------------------------------
# Index over the reused inputs
# ---------------------------------------------------------------------------


def _parent_number(issue: IssueRecord) -> int | None:
    """The parent Issue number, by the same rule the rest of Orchestune uses.

    `effective_parent_number` prefers the native sub-Issue relationship and
    falls back to the body's Footprint `parent_issue_number` — the degraded
    mode for forges that expose no relationship API (#485).  Resolving it here
    the same way keeps a legacy child from being recorded as having no parent,
    which would be an invented absence.  A payload that carries no usable
    number still resolves to `None`.
    """
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


# The observed fields of an Issue, named as a diagnostic reports them.  Sorted
# by name, so a conflict reads the same whatever order the records arrived in.
#
# Parent identity and parent state are separate fields on purpose: a parent
# that transitions between the sequential Issue queries leaves two versions of
# one child naming the same parent with different states.  Folding them
# together would make that a conflict about the link itself, dropping a parent
# scope whose identity was never in doubt.
FIELD_LABELS = "labels"
FIELD_PARENT = "parent"
FIELD_PARENT_STATE = "parent state"
FIELD_STATE = "state"

_ISSUE_FIELDS: tuple[tuple[str, Callable[[IssueRecord], FactValue]], ...] = (
    (FIELD_LABELS, _issue_labels),
    (FIELD_PARENT, _parent_number),
    (FIELD_PARENT_STATE, _parent_state),
    (FIELD_STATE, _issue_state),
)


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
    """Group observed Issues under their parents, keeping every declared state.

    Children are read over several Forge requests, so two of them can carry
    different states for the same parent that transitioned mid-fetch.  Every
    distinct declaration is kept here; picking one arbitrarily would hand a
    parent invariant a confident value with no basis.  A child whose own
    versions disagree about *which* Issue is its parent contributes no link at
    all; one that merely saw the parent in two states still links, and both
    states it saw are kept.
    """
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
    # A parent Issue included so its own state can be read is a parent, not a
    # task: giving it task facts would let a task invariant find (and a repair
    # act on) a missing branch, worktree, or pull request that a parent never
    # has.  Run state or the caller's branch map override this — an Issue that
    # is actually being worked on stays a task even if children point at it.
    # A nested Issue remains a task even when it also parents other Issues.
    # Use every declared version as evidence: conflicting parent identities
    # make the link unknown, but must not erase the task itself.
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


# ---------------------------------------------------------------------------
# Pure resolution of ambiguous correspondences
# ---------------------------------------------------------------------------


def _resolve_execution(
    issue_number: int, records: tuple[ExecutionRecord, ...]
) -> _ExecutionView:
    """Map an Issue onto at most one execution, keeping ambiguity diagnosable."""
    if len(records) <= 1:
        return _ExecutionView(records[0] if records else None)
    branches = ", ".join(repr(record.branch) for record in records)
    return _ExecutionView(
        None,
        (
            f"ambiguous execution correspondence for issue {issue_number}: "
            f"branches {branches}",
        ),
    )


def _resolve_branch(
    issue_number: int, view: _ExecutionView, declared: str | None
) -> _Identifier:
    """Reconcile the caller's branch map with the branch the execution records."""
    recorded = None if view.record is None else view.record.branch
    if declared is not None and recorded is not None and declared != recorded:
        return _Identifier(
            None,
            _UNKNOWN,
            (
                f"ambiguous branch correspondence for issue {issue_number}: "
                f"declared {declared!r}, execution {recorded!r}",
            ),
        )
    if declared is not None:
        return _Identifier(declared)
    if view.ambiguous:
        return _Identifier(None, _UNKNOWN, view.ambiguity)
    if recorded is not None:
        return _Identifier(recorded)
    return _Identifier(None, _UNKNOWN, (_NO_BRANCH,))


def _resolve_worktree_path(view: _ExecutionView) -> _Identifier:
    """A recorded execution without a path is unknown, not a known absence."""
    if view.ambiguous:
        return _Identifier(None, _UNKNOWN, view.ambiguity)
    if view.record is None:
        return _Identifier(None)
    if view.record.worktree_path is None:
        return _Identifier(None, _UNKNOWN, (_NO_WORKTREE_PATH,))
    return _Identifier(view.record.worktree_path)


def _execution_kind(view: _ExecutionView) -> _Reading:
    if view.ambiguous:
        return _Reading(None, _UNKNOWN, view.ambiguity)
    record = view.record
    if record is None:
        return _Reading(EXECUTION_KIND_NONE)
    if record.external_id is not None:
        return _Reading(EXECUTION_KIND_CLOUD)
    if record.pid is not None:
        return _Reading(EXECUTION_KIND_LOCAL)
    return _Reading(EXECUTION_KIND_UNKNOWN, _UNKNOWN, (_NO_EXECUTION_HANDLE,))


def _execution_field(
    view: _ExecutionView, read: Callable[[ExecutionRecord], FactValue]
) -> _Reading:
    if view.ambiguous:
        return _Reading(None, _UNKNOWN, view.ambiguity)
    if view.record is None:
        return _Reading(None)
    return _Reading(read(view.record))


def _read_pid(record: ExecutionRecord) -> FactValue:
    return record.pid


def _read_external_id(record: ExecutionRecord) -> FactValue:
    return record.external_id


def _branch_exists(probe: GitProbe, branch: str) -> FactValue:
    return probe.branch_exists(branch)


def _worktree_exists(probe: WorktreeProbe, path: str) -> FactValue:
    return probe.worktree_exists(path)


# ---------------------------------------------------------------------------
# Collector
# ---------------------------------------------------------------------------


class ObservationCollector:
    """Builds one immutable `ObservedRepositoryState` from injected probes.

    The same probe answers always produce the same snapshot: scopes are emitted
    repository → parent → task with numerically ordered subjects, and each
    scope's facts are ordered by name, so two collections over identical inputs
    compare equal.
    """

    def __init__(
        self,
        *,
        repository_id: str,
        forge_probe: ForgeProbe | None = None,
        git_probe: GitProbe | None = None,
        worktree_probe: WorktreeProbe | None = None,
        process_probe: ProcessProbe | None = None,
        external_probe: ExternalExecutionProbe | None = None,
        clock: Clock | None = None,
        freshness_budget: timedelta | None = None,
    ) -> None:
        self._repository_id = repository_id
        self._forge_probe = forge_probe
        self._git_probe = git_probe
        self._worktree_probe = worktree_probe
        self._process_probe = process_probe
        self._external_probe = external_probe
        self._clock = clock or _default_clock
        self._freshness_budget = freshness_budget

    def collect(
        self,
        *,
        forge: ForgeSnapshot | None = None,
        executions: Sequence[ExecutionRecord] = (),
        branches_by_issue: Mapping[int, str] | None = None,
    ) -> ObservedRepositoryState:
        """Observe the repository without changing any of it."""
        now = self._clock()
        reading = self._resolve_forge(forge, now)
        index = _build_index(reading.snapshot, executions, branches_by_issue)
        scopes = [
            self._repository_scope(reading, index, now),
            *self._parent_scopes(reading, index, now),
            *self._task_scopes(reading, index, now),
        ]
        return ObservedRepositoryState(
            repository_id=self._repository_id,
            observed_at=now,
            observations=tuple(scopes),
        )

    # -- Forge resolution ---------------------------------------------------

    def _resolve_forge(
        self, forge: ForgeSnapshot | None, now: datetime
    ) -> _ForgeReading:
        """Prefer the caller's snapshot; only probe when there is none."""
        if forge is not None:
            return self._grade_freshness(forge, now)
        if self._forge_probe is None:
            return _ForgeReading(
                None, _UNKNOWN, ("forge probe is not configured",), now
            )
        try:
            fetched = self._forge_probe.fetch_snapshot()
        except Exception as exc:  # noqa: BLE001 - an outage is unknown, not empty
            return _ForgeReading(
                None, _UNKNOWN, (f"forge probe failed: {_describe(exc)}",), now
            )
        return self._grade_freshness(fetched, now)

    def _grade_freshness(self, snapshot: ForgeSnapshot, now: datetime) -> _ForgeReading:
        fetched_at = snapshot.fetched_at
        if fetched_at is None:
            return _ForgeReading(snapshot, _KNOWN, (), now)
        budget = self._freshness_budget
        try:
            age = now - fetched_at
        except TypeError:
            if budget is None:
                return _ForgeReading(snapshot, _KNOWN, (), fetched_at)
            return _ForgeReading(
                snapshot,
                _UNKNOWN,
                (
                    "forge snapshot timestamp cannot be compared with the "
                    "collector clock",
                ),
                now,
            )
        if age < timedelta(0):
            return _ForgeReading(
                snapshot,
                _UNKNOWN,
                (f"forge snapshot timestamp is {-age} in the future",),
                now,
            )
        if budget is not None and age > budget:
            return _ForgeReading(
                snapshot,
                _STALE,
                (f"forge snapshot is {age} old, past the {budget} freshness budget",),
                fetched_at,
            )
        return _ForgeReading(snapshot, _KNOWN, (), fetched_at)

    def _forge_reading(self, reading: _ForgeReading, value: FactValue) -> _Reading:
        """Grade a value taken from the snapshot with the snapshot's certainty."""
        if reading.snapshot is None:
            return _Reading(None, _UNKNOWN, reading.diagnostics)
        return _Reading(value, reading.certainty, reading.diagnostics)

    # -- Repository scope ---------------------------------------------------

    def _repository_scope(
        self, reading: _ForgeReading, index: _Index, now: datetime
    ) -> ScopedObservations:
        forge = _emitter(SOURCE_FORGE, reading.observed_at)
        run_state = _emitter(SOURCE_RUN_STATE, now)
        executions = sum(len(records) for records in index.executions.values())
        snapshot = reading.snapshot
        facts = [
            forge(FACT_FORGE_REACHABLE, self._forge_reading(reading, True)),
            forge(
                FACT_ISSUE_COUNT,
                self._complete_count_reading(
                    reading,
                    len(index.issues),
                    complete=snapshot is not None and snapshot.issues_complete,
                    incomplete_diagnostic=_FILTERED_ISSUE_COUNT,
                ),
            ),
            forge(
                FACT_PULL_REQUEST_COUNT,
                self._complete_count_reading(
                    reading,
                    index.pull_request_count,
                    complete=(snapshot is not None and snapshot.pull_requests_complete),
                    incomplete_diagnostic=_FILTERED_PULL_REQUEST_COUNT,
                ),
            ),
        ]
        facts.append(run_state(FACT_EXECUTION_COUNT, _Reading(executions)))
        return _scope(ConsistencyScope.REPOSITORY, None, facts)

    def _complete_count_reading(
        self,
        reading: _ForgeReading,
        value: int,
        *,
        complete: bool,
        incomplete_diagnostic: str,
    ) -> _Reading:
        if reading.snapshot is None or complete:
            return self._forge_reading(reading, value)
        return _unknown_forge_reading(
            reading,
            (incomplete_diagnostic,),
            value,
        )

    # -- Parent scope -------------------------------------------------------

    def _parent_scopes(
        self, reading: _ForgeReading, index: _Index, now: datetime
    ) -> list[ScopedObservations]:
        identity = _emitter(SOURCE_COLLECTOR, now)
        forge = _emitter(SOURCE_FORGE, reading.observed_at)
        return [
            _scope(
                ConsistencyScope.PARENT,
                str(parent),
                [
                    identity(FACT_PARENT_ISSUE_NUMBER, _Reading(parent)),
                    forge(
                        FACT_CHILD_ISSUE_NUMBERS,
                        self._children_reading(parent, reading, index),
                    ),
                    forge(
                        FACT_PARENT_STATE,
                        self._parent_state_reading(parent, reading, index),
                    ),
                ],
            )
            for parent in sorted(index.children_by_parent)
        ]

    def _children_reading(
        self, parent: int, reading: _ForgeReading, index: _Index
    ) -> _Reading:
        """Report the observed children, and whether they are all of them.

        The subset is kept as the value even when uncertain — it is the
        evidence a diagnosis needs — but a filtered snapshot must not let a
        parent invariant conclude that the children it cannot see are gone.
        """
        children = index.children_by_parent[parent]
        if reading.snapshot is not None and not reading.snapshot.issues_complete:
            return _unknown_forge_reading(
                reading,
                (_FILTERED_CHILDREN,),
                children,
            )
        return self._forge_reading(reading, children)

    def _parent_state_reading(
        self, parent: int, reading: _ForgeReading, index: _Index
    ) -> _Reading:
        """Prefer the parent's own record; fall back to what a child declares."""
        view = index.issues.get(parent)
        if view is not None:
            if not view.agrees_on(FIELD_STATE):
                return _unknown_forge_reading(
                    reading,
                    (_conflicting_versions(parent, view),),
                )
            return self._forge_reading(reading, view.records[0].state)
        declared = index.declared_parent_states.get(parent, ())
        if len(declared) > 1:
            candidates = ", ".join(repr(state) for state in declared)
            return _unknown_forge_reading(
                reading,
                (
                    f"ambiguous parent state for issue {parent}: "
                    f"children declare {candidates}",
                ),
            )
        if declared:
            return self._forge_reading(reading, declared[0])
        return _unknown_forge_reading(
            reading,
            (_missing_from_snapshot(parent),),
        )

    # -- Task scope ---------------------------------------------------------

    def _task_scopes(
        self, reading: _ForgeReading, index: _Index, now: datetime
    ) -> list[ScopedObservations]:
        return [
            self._task_scope(issue_number, reading, index, now)
            for issue_number in index.task_numbers
        ]

    def _task_scope(
        self, issue_number: int, reading: _ForgeReading, index: _Index, now: datetime
    ) -> ScopedObservations:
        view = _resolve_execution(issue_number, index.executions.get(issue_number, ()))
        branch = _resolve_branch(
            issue_number, view, index.declared_branches.get(issue_number)
        )
        facts = [
            *self._issue_observations(issue_number, reading, index, now),
            *self._execution_observations(view, now),
            *self._workspace_observations(branch, view, now),
            *self._pull_request_observations(branch, reading, index),
        ]
        return _scope(ConsistencyScope.TASK, str(issue_number), facts)

    def _issue_observations(
        self, issue_number: int, reading: _ForgeReading, index: _Index, now: datetime
    ) -> list[Observation]:
        view = index.issues.get(issue_number)
        identity = _emitter(SOURCE_COLLECTOR, now)
        forge = _emitter(SOURCE_FORGE, reading.observed_at)
        return [
            identity(FACT_ISSUE_NUMBER, _Reading(issue_number)),
            *(
                forge(
                    name,
                    self._issue_reading(issue_number, view, reading, field, value_of),
                )
                for name, field, value_of in (
                    (FACT_ISSUE_STATE, FIELD_STATE, _issue_state),
                    (FACT_ISSUE_LABELS, FIELD_LABELS, _issue_labels),
                    (FACT_ISSUE_STATUS_LABELS, FIELD_LABELS, _issue_status_labels),
                    (FACT_PARENT_ISSUE_NUMBER, FIELD_PARENT, _parent_number),
                )
            ),
        ]

    def _issue_reading(
        self,
        issue_number: int,
        view: _IssueView | None,
        reading: _ForgeReading,
        field: str,
        value_of: Callable[[IssueRecord], FactValue],
    ) -> _Reading:
        """Read one Issue field, per field, so a conflict clouds only itself."""
        if reading.snapshot is None:
            return _Reading(None, _UNKNOWN, reading.diagnostics)
        if view is None:
            if reading.snapshot.issues_complete:
                return self._forge_reading(reading, None)
            return _unknown_forge_reading(
                reading,
                (_missing_from_snapshot(issue_number),),
            )
        if not view.agrees_on(field):
            return _unknown_forge_reading(
                reading,
                (_conflicting_versions(issue_number, view),),
            )
        return _Reading(
            value_of(view.records[0]), reading.certainty, reading.diagnostics
        )

    def _execution_observations(
        self, view: _ExecutionView, now: datetime
    ) -> list[Observation]:
        run_state = _emitter(SOURCE_RUN_STATE, now)
        return [
            run_state(FACT_EXECUTION_KIND, _execution_kind(view)),
            run_state(FACT_EXECUTION_PID, _execution_field(view, _read_pid)),
            run_state(
                FACT_EXECUTION_EXTERNAL_ID, _execution_field(view, _read_external_id)
            ),
            _emitter(SOURCE_PROCESS, now)(
                FACT_EXECUTION_PROCESS_ALIVE, self._process_reading(view)
            ),
            _emitter(SOURCE_EXTERNAL, now)(
                FACT_EXECUTION_EXTERNAL_STATUS, self._external_reading(view)
            ),
        ]

    def _process_reading(self, view: _ExecutionView) -> _Reading:
        """Separate "no process by design" from "the process is gone".

        A cloud execution legitimately has `pid=None`; reporting it as a dead
        process would let a zombie repair reclaim a run that is still going.
        """
        if view.ambiguous:
            return _Reading(None, _UNKNOWN, view.ambiguity)
        record = view.record
        if record is None:
            return _Reading(None)
        if record.external_id is not None:
            return _Reading(None, _UNKNOWN, (_NO_LOCAL_PROCESS,))
        if record.pid is None:
            return _Reading(None, _UNKNOWN, (_NO_LOCAL_PID,))
        pid = record.pid
        return _read_probe(
            "process", self._process_probe, lambda probe: probe.is_alive(pid)
        )

    def _external_reading(self, view: _ExecutionView) -> _Reading:
        if view.ambiguous:
            return _Reading(None, _UNKNOWN, view.ambiguity)
        record = view.record
        if record is None or record.external_id is None:
            return _Reading(None)
        external_id = record.external_id
        return _read_probe(
            "external execution",
            self._external_probe,
            lambda probe: probe.status(external_id),
        )

    def _workspace_observations(
        self, branch: _Identifier, view: _ExecutionView, now: datetime
    ) -> list[Observation]:
        path = _resolve_worktree_path(view)
        run_state = _emitter(SOURCE_RUN_STATE, now)
        return [
            run_state(FACT_BRANCH_NAME, branch.as_reading()),
            _emitter(SOURCE_GIT, now)(
                FACT_BRANCH_EXISTS,
                _existence_reading(branch, "git", self._git_probe, _branch_exists),
            ),
            run_state(FACT_WORKTREE_PATH, path.as_reading()),
            _emitter(SOURCE_WORKTREE, now)(
                FACT_WORKTREE_EXISTS,
                _existence_reading(
                    path, "worktree", self._worktree_probe, _worktree_exists
                ),
            ),
        ]

    def _pull_request_observations(
        self, branch: _Identifier, reading: _ForgeReading, index: _Index
    ) -> list[Observation]:
        number, state, head_ref, base_ref = self._pull_request_readings(
            branch, reading, index
        )
        forge = _emitter(SOURCE_FORGE, reading.observed_at)
        return [
            forge(FACT_PULL_REQUEST_NUMBER, number),
            forge(FACT_PULL_REQUEST_STATE, state),
            forge(FACT_PULL_REQUEST_HEAD_REF, head_ref),
            forge(FACT_PULL_REQUEST_BASE_REF, base_ref),
        ]

    def _pull_request_readings(
        self, branch: _Identifier, reading: _ForgeReading, index: _Index
    ) -> tuple[_Reading, _Reading, _Reading, _Reading]:
        """Link a task to its pull request, or record why the link is unclear."""
        if reading.snapshot is None:
            unknown = _Reading(None, _UNKNOWN, reading.diagnostics)
            return unknown, unknown, unknown, unknown
        if branch.value is None:
            unknown = _unknown_forge_reading(reading, branch.diagnostics)
            return unknown, unknown, unknown, unknown
        candidates = index.pull_requests_by_branch.get(branch.value, ())
        if len(candidates) > 1:
            numbers = ", ".join(str(record.number) for record in candidates)
            unknown = _unknown_forge_reading(
                reading,
                (
                    "ambiguous pull request correspondence for branch "
                    f"{branch.value!r}: candidates {numbers}",
                ),
            )
            return unknown, unknown, unknown, unknown
        if not candidates:
            if not reading.snapshot.pull_requests_complete:
                unknown = _unknown_forge_reading(
                    reading,
                    (_FILTERED_PULL_REQUESTS,),
                )
                return unknown, unknown, unknown, unknown
            return (
                self._forge_reading(reading, None),
                self._forge_reading(reading, None),
                self._forge_reading(reading, None),
                self._forge_reading(reading, None),
            )
        return (
            self._forge_reading(reading, candidates[0].number),
            self._forge_reading(reading, candidates[0].state),
            self._forge_reading(reading, candidates[0].head_ref),
            self._forge_reading(reading, candidates[0].base_ref),
        )


def _missing_from_snapshot(issue_number: int) -> str:
    return f"issue #{issue_number} is not present in the reused Forge snapshot"


def _conflicting_versions(issue_number: int, view: _IssueView) -> str:
    return (
        f"the reused Forge snapshot holds {len(view.records)} versions of issue "
        f"#{issue_number} that disagree on {', '.join(view.conflicts)}"
    )


def build_observed_repository_state(
    *,
    repository_id: str,
    forge: ForgeSnapshot | None = None,
    executions: Sequence[ExecutionRecord] = (),
    branches_by_issue: Mapping[int, str] | None = None,
    forge_probe: ForgeProbe | None = None,
    git_probe: GitProbe | None = None,
    worktree_probe: WorktreeProbe | None = None,
    process_probe: ProcessProbe | None = None,
    external_probe: ExternalExecutionProbe | None = None,
    clock: Clock | None = None,
    freshness_budget: timedelta | None = None,
) -> ObservedRepositoryState:
    """Collect one snapshot without holding on to a collector."""
    return ObservationCollector(
        repository_id=repository_id,
        forge_probe=forge_probe,
        git_probe=git_probe,
        worktree_probe=worktree_probe,
        process_probe=process_probe,
        external_probe=external_probe,
        clock=clock,
        freshness_budget=freshness_budget,
    ).collect(forge=forge, executions=executions, branches_by_issue=branches_by_issue)
