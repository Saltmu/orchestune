"""Pin the typed dependency boundary used by cycle execution."""

from __future__ import annotations

import dataclasses
import functools
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_CACHE_DIR = _REPO_ROOT / ".pytest_cache" / "typing_cache"


@functools.cache
def _run_mypy(source: str) -> subprocess.CompletedProcess[str]:
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
    cache_file = _CACHE_DIR / f"{digest}.json"
    lock_file = _CACHE_DIR / f"{digest}.lock"

    if cache_file.is_file():
        try:
            data = json.loads(cache_file.read_text(encoding="utf-8"))
            return subprocess.CompletedProcess(
                args=[sys.executable, "-m", "mypy", "--no-incremental", "-c", source],
                returncode=data["returncode"],
                stdout=data["stdout"],
                stderr=data["stderr"],
            )
        except Exception:
            pass

    # If another worker is already running mypy for this exact fixture, wait for it
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if lock_file.is_file():
        import time

        for _ in range(300):
            time.sleep(0.05)
            if cache_file.is_file():
                try:
                    data = json.loads(cache_file.read_text(encoding="utf-8"))
                    return subprocess.CompletedProcess(
                        args=[
                            sys.executable,
                            "-m",
                            "mypy",
                            "--no-incremental",
                            "-c",
                            source,
                        ],
                        returncode=data["returncode"],
                        stdout=data["stdout"],
                        stderr=data["stderr"],
                    )
                except Exception:
                    pass
            if not lock_file.is_file():
                break

    try:
        lock_file.write_text(str(sys.argv), encoding="utf-8")
    except Exception:
        pass

    try:
        proc = subprocess.run(
            [sys.executable, "-m", "mypy", "--no-incremental", "-c", source],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
        )
        try:
            tmp_file = cache_file.with_suffix(".tmp")
            tmp_file.write_text(
                json.dumps(
                    {
                        "returncode": proc.returncode,
                        "stdout": proc.stdout,
                        "stderr": proc.stderr,
                    }
                ),
                encoding="utf-8",
            )
            tmp_file.replace(cache_file)
        except Exception:
            pass
        return proc
    finally:
        try:
            if lock_file.is_file():
                lock_file.unlink()
        except Exception:
            pass


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
    result = _run_mypy(_COMBINED_FIXTURE)
    errors = [line for line in result.stdout.splitlines() if ": error:" in line]
    # The valid assignment must introduce zero errors; exactly the 3 negative cases fail.
    assert len(errors) == 3, result.stdout + result.stderr
    assert not any(
        "Incompatible types in assignment" in line for line in errors
    ), result.stdout
    assert "Cannot find implementation or library stub" not in result.stdout


def test_mypy_rejects_wrong_callback_arguments_and_returns() -> None:
    result = _run_mypy(_COMBINED_FIXTURE)
    errors = [line for line in result.stdout.splitlines() if ": error:" in line]

    assert result.returncode != 0, result.stdout + result.stderr
    assert len(errors) == 3, result.stdout + result.stderr
    for field in ("prepare_context", "run_gc", "build_report"):
        assert any(field in line for line in errors), result.stdout + result.stderr
    assert "Cannot find implementation or library stub" not in result.stdout


# Keep negative examples outside tests/ modules so `mypy orchestune tests` can
# validate the production boundary without intentionally broken declarations.
# Both valid and negative fixtures are combined to batch typechecking into a
# single mypy subprocess invocation.
_COMBINED_FIXTURE = """\
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

valid_dependencies: CycleExecutionDependencies[_ConsistencyRuntime | None, _RepairCycleState]
valid_dependencies = _build_execution_dependencies()

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

_VALID_FIXTURE = _COMBINED_FIXTURE
_INVALID_FIXTURE = _COMBINED_FIXTURE
