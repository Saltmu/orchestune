"""Observation collector aggregating facts across forge, process, git, and workspace."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, runtime_checkable

from orchestune.consistency.models import (
    ConsistencyScope,
    ObservedRepositoryState,
    ScopedObservations,
)
from orchestune.consistency.observation_execution import (
    EXECUTION_KIND_CLOUD,
    EXECUTION_KIND_LOCAL,
    EXECUTION_KIND_NONE,
    EXECUTION_KIND_UNKNOWN,
    ExecutionReader,
    _resolve_branch,
    _resolve_execution,
)
from orchestune.consistency.observation_issues import (
    issue_observations,
    parent_scopes,
)
from orchestune.consistency.observation_pull_requests import (
    pull_request_observations,
)
from orchestune.consistency.observation_readings import (
    _KNOWN,
    _STALE,
    _UNKNOWN,
    SOURCE_FORGE,
    SOURCE_RUN_STATE,
    ExecutionRecord,
    ForgeSnapshot,
    _build_index,
    _describe,
    _emitter,
    _ForgeReading,
    _Index,
    _Reading,
    _scope,
    _unknown_forge_reading,
    forge_reading,
)
from orchestune.consistency.vocabulary import (
    FACT_BRANCH_EXISTS,
    FACT_BRANCH_NAME,
    FACT_CHILD_ISSUE_NUMBERS,
    FACT_EXECUTION_CLAIM_ID,
    FACT_EXECUTION_CLAIM_STAGE,
    FACT_EXECUTION_COUNT,
    FACT_EXECUTION_EXTERNAL_ID,
    FACT_EXECUTION_EXTERNAL_STATUS,
    FACT_EXECUTION_KIND,
    FACT_EXECUTION_LAUNCH_PHASE,
    FACT_EXECUTION_OWNER_KIND,
    FACT_EXECUTION_PID,
    FACT_EXECUTION_PROCESS_ALIVE,
    FACT_EXECUTION_STARTED_AT,
    FACT_FORGE_REACHABLE,
    FACT_ISSUE_COUNT,
    FACT_ISSUE_LABELS,
    FACT_ISSUE_NUMBER,
    FACT_ISSUE_STATE,
    FACT_ISSUE_STATUS_LABELS,
    FACT_PARENT_ISSUE_NUMBER,
    FACT_PARENT_STATE,
    FACT_PULL_REQUEST_BASE_REF,
    FACT_PULL_REQUEST_COUNT,
    FACT_PULL_REQUEST_HEAD_REF,
    FACT_PULL_REQUEST_NUMBER,
    FACT_PULL_REQUEST_STATE,
    FACT_WORKTREE_EXISTS,
    FACT_WORKTREE_PATH,
)

__all__ = [
    "Clock",
    "EXECUTION_KIND_CLOUD",
    "EXECUTION_KIND_LOCAL",
    "EXECUTION_KIND_NONE",
    "EXECUTION_KIND_UNKNOWN",
    "ExecutionRecord",
    "ExternalExecutionProbe",
    "FACT_BRANCH_EXISTS",
    "FACT_BRANCH_NAME",
    "FACT_CHILD_ISSUE_NUMBERS",
    "FACT_EXECUTION_CLAIM_ID",
    "FACT_EXECUTION_CLAIM_STAGE",
    "FACT_EXECUTION_COUNT",
    "FACT_EXECUTION_EXTERNAL_ID",
    "FACT_EXECUTION_EXTERNAL_STATUS",
    "FACT_EXECUTION_KIND",
    "FACT_EXECUTION_LAUNCH_PHASE",
    "FACT_EXECUTION_OWNER_KIND",
    "FACT_EXECUTION_PID",
    "FACT_EXECUTION_PROCESS_ALIVE",
    "FACT_EXECUTION_STARTED_AT",
    "FACT_FORGE_REACHABLE",
    "FACT_ISSUE_COUNT",
    "FACT_ISSUE_LABELS",
    "FACT_ISSUE_NUMBER",
    "FACT_ISSUE_STATE",
    "FACT_ISSUE_STATUS_LABELS",
    "FACT_PARENT_ISSUE_NUMBER",
    "FACT_PARENT_STATE",
    "FACT_PULL_REQUEST_BASE_REF",
    "FACT_PULL_REQUEST_COUNT",
    "FACT_PULL_REQUEST_HEAD_REF",
    "FACT_PULL_REQUEST_NUMBER",
    "FACT_PULL_REQUEST_STATE",
    "FACT_WORKTREE_EXISTS",
    "FACT_WORKTREE_PATH",
    "ForgeProbe",
    "ForgeSnapshot",
    "GitProbe",
    "ObservationCollector",
    "ObservationProbes",
    "ProcessProbe",
    "WorktreeProbe",
    "build_observed_repository_state",
]

_FILTERED_ISSUE_COUNT = (
    "the reused Issue snapshot is filtered, so the observed issue count is not "
    "a complete repository count"
)
_FILTERED_PULL_REQUEST_COUNT = (
    "the reused pull request snapshot is filtered, so the observed pull request "
    "count is not a complete repository count"
)


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


@dataclass(frozen=True)
class ObservationProbes:
    """Probes injected into repository observation."""

    forge: ForgeProbe | None = None
    git: GitProbe | None = None
    worktree: WorktreeProbe | None = None
    process: ProcessProbe | None = None
    external: ExternalExecutionProbe | None = None

    @property
    def forge_probe(self) -> ForgeProbe | None:
        return self.forge

    @property
    def git_probe(self) -> GitProbe | None:
        return self.git

    @property
    def worktree_probe(self) -> WorktreeProbe | None:
        return self.worktree

    @property
    def process_probe(self) -> ProcessProbe | None:
        return self.process

    @property
    def external_probe(self) -> ExternalExecutionProbe | None:
        return self.external


Clock = Callable[[], datetime]


def _default_clock() -> datetime:
    return datetime.now(UTC)


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
        probes: ObservationProbes | None = None,
        clock: Clock | None = None,
        freshness_budget: timedelta | None = None,
        **kwargs: Any,
    ) -> None:
        self._repository_id = repository_id
        forge_probe = kwargs.pop("forge_probe", None)
        git_probe = kwargs.pop("git_probe", None)
        worktree_probe = kwargs.pop("worktree_probe", None)
        process_probe = kwargs.pop("process_probe", None)
        external_probe = kwargs.pop("external_probe", None)
        if kwargs:
            unexpected = ", ".join(sorted(kwargs.keys()))
            raise TypeError(
                f"ObservationCollector() got unexpected keyword argument(s): {unexpected}"
            )
        if probes is not None:
            self._forge_probe = forge_probe or probes.forge
            self._git_probe = git_probe or probes.git
            self._worktree_probe = worktree_probe or probes.worktree
            self._process_probe = process_probe or probes.process
            self._external_probe = external_probe or probes.external
        else:
            self._forge_probe = forge_probe
            self._git_probe = git_probe
            self._worktree_probe = worktree_probe
            self._process_probe = process_probe
            self._external_probe = external_probe
        self._execution_reader = ExecutionReader(
            process_probe=self._process_probe,
            external_probe=self._external_probe,
            git_probe=self._git_probe,
            worktree_probe=self._worktree_probe,
        )
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
            *parent_scopes(reading, index, now),
            *self._task_scopes(reading, index, now),
        ]
        return ObservedRepositoryState(
            repository_id=self._repository_id,
            observed_at=now,
            observations=tuple(scopes),
        )

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
        except Exception as exc:  # noqa: BLE001 - boundary: Forge outages become unknown snapshots with diagnostics
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

    def _repository_scope(
        self, reading: _ForgeReading, index: _Index, now: datetime
    ) -> ScopedObservations:
        forge = _emitter(SOURCE_FORGE, reading.observed_at)
        run_state = _emitter(SOURCE_RUN_STATE, now)
        executions = sum(len(records) for records in index.executions.values())
        snapshot = reading.snapshot
        facts = [
            forge(FACT_FORGE_REACHABLE, forge_reading(reading, True)),
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
            return forge_reading(reading, value)
        return _unknown_forge_reading(
            reading,
            (incomplete_diagnostic,),
            value,
        )

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
            *issue_observations(issue_number, reading, index, now),
            *self._execution_reader.execution_observations(view, now),
            *self._execution_reader.workspace_observations(branch, view, now),
            *pull_request_observations(branch, reading, index),
        ]
        return _scope(ConsistencyScope.TASK, str(issue_number), facts)


# ---------------------------------------------------------------------------
# Module entry point
# ---------------------------------------------------------------------------


def build_observed_repository_state(
    *,
    repository_id: str,
    forge: ForgeSnapshot | None = None,
    executions: Sequence[ExecutionRecord] = (),
    branches_by_issue: Mapping[int, str] | None = None,
    probes: ObservationProbes | None = None,
    clock: Clock | None = None,
    freshness_budget: timedelta | None = None,
    **kwargs: Any,
) -> ObservedRepositoryState:
    """Collect one snapshot without holding on to a collector."""
    probe_keys = {
        "forge_probe",
        "git_probe",
        "worktree_probe",
        "process_probe",
        "external_probe",
        "forge",
        "git",
        "worktree",
        "process",
        "external",
    }
    if probes is None and any(k in kwargs for k in probe_keys):
        probes = ObservationProbes(
            forge=kwargs.pop("forge_probe", kwargs.pop("forge", None)),
            git=kwargs.pop("git_probe", kwargs.pop("git", None)),
            worktree=kwargs.pop("worktree_probe", kwargs.pop("worktree", None)),
            process=kwargs.pop("process_probe", kwargs.pop("process", None)),
            external=kwargs.pop("external_probe", kwargs.pop("external", None)),
        )
    if kwargs:
        unexpected = ", ".join(sorted(kwargs.keys()))
        raise TypeError(
            f"build_observed_repository_state() got unexpected keyword argument(s): {unexpected}"
        )
    return ObservationCollector(
        repository_id=repository_id,
        probes=probes,
        clock=clock,
        freshness_budget=freshness_budget,
    ).collect(forge=forge, executions=executions, branches_by_issue=branches_by_issue)
