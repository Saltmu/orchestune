"""Pin the typed dependency boundary used by cycle execution."""

from __future__ import annotations

import dataclasses
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _run_mypy(source: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "mypy", "--no-incremental", "-c", source],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
    )


def test_cycle_dependency_builder_returns_frozen_contract() -> None:
    from dataclasses import FrozenInstanceError

    from orchestune.dispatch.cycle import _build_execution_dependencies
    from orchestune.dispatch.cycle_execution_contracts import (
        CycleExecutionDependencies,
    )

    dependencies = _build_execution_dependencies()
    assert isinstance(dependencies, CycleExecutionDependencies)
    assert dataclasses.is_dataclass(dependencies)
    assert [field.name for field in dataclasses.fields(dependencies)] == [
        "load_state",
        "now",
        "state_lock",
        "prepare_context",
        "start_consistency",
        "new_repair_state",
        "run_pipeline",
        "finish_consistency",
        "notify_pr_links",
        "run_gc",
        "reconcile_before_scheduling",
        "schedule",
        "build_report",
        "build_event_log",
        "append_event_log",
    ]
    with pytest.raises(FrozenInstanceError):
        field_name = "now"
        setattr(dependencies, field_name, lambda: 0.0)


def test_cycle_builder_is_assignable_to_generic_dependency_contract() -> None:
    result = _run_mypy(_VALID_FIXTURE)
    assert result.returncode == 0, result.stdout + result.stderr


def test_mypy_rejects_wrong_callback_arguments_and_returns() -> None:
    result = _run_mypy(_INVALID_FIXTURE)
    errors = [line for line in result.stdout.splitlines() if ": error:" in line]

    assert result.returncode != 0, result.stdout + result.stderr
    assert len(errors) == 3, result.stdout + result.stderr
    for field in ("prepare_context", "run_gc", "build_report"):
        assert any(field in line for line in errors), result.stdout + result.stderr
    assert "Cannot find implementation or library stub" not in result.stdout


_VALID_FIXTURE = """\
from orchestune.dispatch.cycle import (
    _ConsistencyRuntime,
    _RepairCycleState,
    _build_execution_dependencies,
)
from orchestune.dispatch.cycle_execution_contracts import CycleExecutionDependencies

dependencies: CycleExecutionDependencies[_ConsistencyRuntime | None, _RepairCycleState]
dependencies = _build_execution_dependencies()
"""


# Keep negative examples outside tests/ modules so `mypy orchestune tests` can
# validate the production boundary without intentionally broken declarations.
_INVALID_FIXTURE = """\
from orchestune.consistency.supervisor import ConsistencyCycleReport
from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.cycle import (
    _ConsistencyRuntime,
    _RepairCycleState,
    _build_execution_dependencies,
)
from orchestune.dispatch.cycle_context import CycleContext, IssuesByStatus
from orchestune.dispatch.cycle_events import CompletionEvent, DeviationEvent, PromotionEvent
from orchestune.dispatch.cycle_execution_contracts import CycleExecutionDependencies
from orchestune.dispatch.cycle_report import CycleReport
from orchestune.dispatch.phase_scheduling import SchedulingPhaseResult
from orchestune.dispatch.prior_parent_merge import PriorParentMergeReconciliation
from orchestune.lock_contracts import ExternalLockScanResult
from orchestune.ledger.run_state import RunState

base: CycleExecutionDependencies[_ConsistencyRuntime | None, _RepairCycleState]
base = _build_execution_dependencies()

def bad_prepare(run_state: RunState, config: DispatcherConfig, now: float) -> None:
    raise NotImplementedError

def bad_run_gc(
    ctx: CycleContext, config: DispatcherConfig, completion_events: list[CompletionEvent]
) -> list[CompletionEvent]:
    raise NotImplementedError

def bad_build_report(
    scheduling: SchedulingPhaseResult,
    lock_result: ExternalLockScanResult,
    *,
    deviation_events: list[DeviationEvent],
    completion_events: list[CompletionEvent],
    promotion_event: list[PromotionEvent],
    applied: bool,
) -> CycleReport:
    raise NotImplementedError

CycleExecutionDependencies(
        load_state=base.load_state,
        now=base.now,
        state_lock=base.state_lock,
        prepare_context=bad_prepare,
        start_consistency=base.start_consistency,
        new_repair_state=base.new_repair_state,
        run_pipeline=base.run_pipeline,
        finish_consistency=base.finish_consistency,
        notify_pr_links=base.notify_pr_links,
        run_gc=base.run_gc,
        reconcile_before_scheduling=base.reconcile_before_scheduling,
        schedule=base.schedule,
        build_report=base.build_report,
        build_event_log=base.build_event_log,
        append_event_log=base.append_event_log,
    )

CycleExecutionDependencies(
    load_state=base.load_state,
    now=base.now,
    state_lock=base.state_lock,
    prepare_context=base.prepare_context,
    start_consistency=base.start_consistency,
    new_repair_state=base.new_repair_state,
    run_pipeline=base.run_pipeline,
    finish_consistency=base.finish_consistency,
    notify_pr_links=base.notify_pr_links,
    run_gc=bad_run_gc,
    reconcile_before_scheduling=base.reconcile_before_scheduling,
    schedule=base.schedule,
    build_report=base.build_report,
    build_event_log=base.build_event_log,
    append_event_log=base.append_event_log,
)

CycleExecutionDependencies(
    load_state=base.load_state,
    now=base.now,
    state_lock=base.state_lock,
    prepare_context=base.prepare_context,
    start_consistency=base.start_consistency,
    new_repair_state=base.new_repair_state,
    run_pipeline=base.run_pipeline,
    finish_consistency=base.finish_consistency,
    notify_pr_links=base.notify_pr_links,
    run_gc=base.run_gc,
    reconcile_before_scheduling=base.reconcile_before_scheduling,
    schedule=base.schedule,
    build_report=bad_build_report,
    build_event_log=base.build_event_log,
    append_event_log=base.append_event_log,
)
"""
