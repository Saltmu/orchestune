"""Dependency-inversion boundaries for consistency observation and repair."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from orchestune.consistency.models import (
    ConsistencyFinding,
    ConsistencyReport,
    ConsistencyScope,
    DesiredRepositoryState,
    ObservedRepositoryState,
    RepairCommand,
    RepairResult,
)


@runtime_checkable
class Observer(Protocol):
    """Normalizes external facts without applying repairs."""

    def observe(self) -> ObservedRepositoryState: ...


@runtime_checkable
class Invariant(Protocol):
    """Pure evaluator for one stable invariant."""

    code: str
    scope: ConsistencyScope

    def evaluate(
        self,
        observed: ObservedRepositoryState,
        desired: DesiredRepositoryState,
    ) -> tuple[ConsistencyFinding, ...]: ...


@runtime_checkable
class RepairPlanner(Protocol):
    """Purely converts findings into bounded, typed repair commands."""

    def plan(self, report: ConsistencyReport) -> tuple[RepairCommand, ...]: ...


@runtime_checkable
class RepairExecutor(Protocol):
    """Explicit side-effect boundary for applying one repair command."""

    def execute(self, command: RepairCommand) -> RepairResult: ...
