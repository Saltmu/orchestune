"""Execution and workspace observation readers."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from orchestune.consistency.models import FactValue, Observation
from orchestune.consistency.observation_readings import (
    _UNKNOWN,
    SOURCE_EXTERNAL,
    SOURCE_GIT,
    SOURCE_PROCESS,
    SOURCE_RUN_STATE,
    SOURCE_WORKTREE,
    ExecutionRecord,
    _emitter,
    _existence_reading,
    _Identifier,
    _read_probe,
    _Reading,
)
from orchestune.consistency.vocabulary import (
    FACT_BRANCH_EXISTS,
    FACT_BRANCH_NAME,
    FACT_EXECUTION_CLAIM_ID,
    FACT_EXECUTION_CLAIM_STAGE,
    FACT_EXECUTION_EXTERNAL_ID,
    FACT_EXECUTION_EXTERNAL_STATUS,
    FACT_EXECUTION_KIND,
    FACT_EXECUTION_LAUNCH_PHASE,
    FACT_EXECUTION_OWNER_KIND,
    FACT_EXECUTION_PID,
    FACT_EXECUTION_PROCESS_ALIVE,
    FACT_EXECUTION_STARTED_AT,
    FACT_WORKTREE_EXISTS,
    FACT_WORKTREE_PATH,
)

# Values of `FACT_EXECUTION_KIND`.  `EXECUTION_KIND_NONE` is a known absence
# (run state records nothing in flight); `EXECUTION_KIND_UNKNOWN` is a record
# that names no handle at all, which is not the same thing.
EXECUTION_KIND_CLOUD = "cloud"
EXECUTION_KIND_LOCAL = "local"
EXECUTION_KIND_NONE = "none"
EXECUTION_KIND_UNKNOWN = "unknown"

_NO_EXECUTION_HANDLE = (
    "execution records neither a local pid nor an external execution id"
)
_NO_LOCAL_PID = "execution records no local pid"
_NO_LOCAL_PROCESS = "cloud execution has no local process"
_NO_WORKTREE_PATH = "execution records no worktree path"
_NO_BRANCH = "no branch is recorded for this task"


# ---------------------------------------------------------------------------
# Execution and workspace view models
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _ExecutionView:
    """The single execution a task maps to, or why that mapping is ambiguous."""

    record: ExecutionRecord | None = None
    ambiguity: tuple[str, ...] = ()

    @property
    def ambiguous(self) -> bool:
        return bool(self.ambiguity)


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
    if record.kind in {EXECUTION_KIND_CLOUD, EXECUTION_KIND_LOCAL}:
        return _Reading(record.kind)
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


def _read_started_at(record: ExecutionRecord) -> FactValue:
    return record.started_at


def _read_owner_kind(record: ExecutionRecord) -> FactValue:
    return record.owner_kind


def _read_claim_id(record: ExecutionRecord) -> FactValue:
    return record.claim_id


def _read_claim_stage(record: ExecutionRecord) -> FactValue:
    return record.claim_stage


def _read_launch_phase(record: ExecutionRecord) -> FactValue:
    return record.launch_phase


def _branch_exists(probe: Any, branch: str) -> FactValue:
    return bool(probe.branch_exists(branch))


def _worktree_exists(probe: Any, path: str) -> FactValue:
    return bool(probe.worktree_exists(path))


# ---------------------------------------------------------------------------
# ExecutionReader
# ---------------------------------------------------------------------------


class ExecutionReader:
    """Reads execution state and workspace facts using injected probes."""

    def __init__(
        self,
        process_probe: Any = None,
        external_probe: Any = None,
        git_probe: Any = None,
        worktree_probe: Any = None,
    ) -> None:
        self._process_probe = process_probe
        self._external_probe = external_probe
        self._git_probe = git_probe
        self._worktree_probe = worktree_probe

    def execution_observations(
        self, view: _ExecutionView, now: datetime
    ) -> list[Observation]:
        run_state = _emitter(SOURCE_RUN_STATE, now)
        return [
            run_state(FACT_EXECUTION_KIND, _execution_kind(view)),
            run_state(FACT_EXECUTION_PID, _execution_field(view, _read_pid)),
            run_state(
                FACT_EXECUTION_STARTED_AT, _execution_field(view, _read_started_at)
            ),
            run_state(
                FACT_EXECUTION_EXTERNAL_ID, _execution_field(view, _read_external_id)
            ),
            run_state(
                FACT_EXECUTION_OWNER_KIND, _execution_field(view, _read_owner_kind)
            ),
            run_state(FACT_EXECUTION_CLAIM_ID, _execution_field(view, _read_claim_id)),
            run_state(
                FACT_EXECUTION_CLAIM_STAGE, _execution_field(view, _read_claim_stage)
            ),
            run_state(
                FACT_EXECUTION_LAUNCH_PHASE, _execution_field(view, _read_launch_phase)
            ),
            _emitter(SOURCE_PROCESS, now)(
                FACT_EXECUTION_PROCESS_ALIVE, self.process_reading(view)
            ),
            _emitter(SOURCE_EXTERNAL, now)(
                FACT_EXECUTION_EXTERNAL_STATUS, self.external_reading(view)
            ),
        ]

    def process_reading(self, view: _ExecutionView) -> _Reading:
        """Separate 'no process by design' from 'the process is gone'."""
        if view.ambiguous:
            return _Reading(None, _UNKNOWN, view.ambiguity)
        record = view.record
        if record is None:
            return _Reading(None)
        if record.kind == EXECUTION_KIND_CLOUD or record.external_id is not None:
            return _Reading(None, _UNKNOWN, (_NO_LOCAL_PROCESS,))
        if record.pid is None:
            return _Reading(None, _UNKNOWN, (_NO_LOCAL_PID,))
        pid = record.pid
        return _read_probe(
            "process", self._process_probe, lambda probe: probe.is_alive(pid)
        )

    def external_reading(self, view: _ExecutionView) -> _Reading:
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

    def workspace_observations(
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
