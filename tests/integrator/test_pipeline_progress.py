"""Tests for step-level progress emission in ``IntegrationPipeline`` (#1866)."""

from __future__ import annotations

import io
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from orchestune.dispatch.config.model import DispatcherConfig
from orchestune.dispatch.pre_scheduling_integration import _build_integrator_config
from orchestune.dispatch.reporting.progress import StdoutProgress
from orchestune.dispatch.reporting.progress_output import RichDispatchProgress
from orchestune.forge import Forge
from orchestune.integrator import (
    IntegrationComponent,
    IntegrationContext,
    IntegrationPipeline,
    IntegrationReport,
    IntegrationStatus,
    IntegratorConfig,
    MultiIssueIntegrator,
)
from orchestune.integrator.execution import (
    ExecutionFailure,
    IntegrationExecutionAbort,
)
from orchestune.integrator.timeout_policy import ExecutionFailureCause


class _RecordingProgress:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, int | None, str | None]] = []

    def emit(
        self,
        phase: str,
        event: str,
        *,
        task_issue: int | None = None,
        reason: str | None = None,
    ) -> None:
        self.events.append((phase, event, task_issue, reason))


def _make_config(progress: object | None = None) -> IntegratorConfig:
    return IntegratorConfig(
        parent_issue_number=100,
        repository_root=Path("."),
        forge=MagicMock(spec=Forge),
        progress=progress,  # type: ignore[arg-type]
    )


def _make_context(config: IntegratorConfig) -> IntegrationContext:
    return IntegrationContext(
        config=config,
        repository_root=config.repository_root,
        original_root=config.repository_root,
        base_branch=config.base_branch,
        temp_branch=config.temp_branch,
    )


class _AlphaStep(IntegrationComponent):
    def execute(self, ctx: IntegrationContext) -> IntegrationReport:
        return {"status": IntegrationStatus.SUCCESS}


class _BetaStep(IntegrationComponent):
    def execute(self, ctx: IntegrationContext) -> IntegrationReport:
        return {"status": IntegrationStatus.SUCCESS}


class _StatusStep(IntegrationComponent):
    def __init__(self, status: IntegrationStatus, error: str | None = None) -> None:
        self._status = status
        self._error = error

    def execute(self, ctx: IntegrationContext) -> IntegrationReport:
        report: IntegrationReport = {"status": self._status}
        if self._error is not None:
            report["error"] = self._error
        return report


class _AbortStep(IntegrationComponent):
    def execute(self, ctx: IntegrationContext) -> IntegrationReport:
        raise IntegrationExecutionAbort(
            ExecutionFailure(
                parent_issue_number=100,
                cause=ExecutionFailureCause.CYCLE_DEADLINE_EXCEEDED,
                stage="MergeAndTestStep",
                detail="timed out",
            )
        )


class _RaiseStep(IntegrationComponent):
    def execute(self, ctx: IntegrationContext) -> IntegrationReport:
        raise RuntimeError("boom")


def test_pipeline_emits_started_and_completed_for_each_step() -> None:
    progress = _RecordingProgress()
    ctx = _make_context(_make_config(progress=progress))

    result = IntegrationPipeline([_AlphaStep(), _BetaStep()]).execute(ctx)

    assert result["status"] == IntegrationStatus.SUCCESS
    assert progress.events == [
        ("_AlphaStep", "started", None, None),
        ("_AlphaStep", "completed", None, None),
        ("_BetaStep", "started", None, None),
        ("_BetaStep", "completed", None, None),
    ]


@pytest.mark.parametrize(
    ("status", "expected_event"),
    [
        (IntegrationStatus.NO_DONE_TASKS, "completed"),
        (IntegrationStatus.SEMANTIC_REVIEW_PENDING, "completed"),
        (IntegrationStatus.REVIEW_GATE_BLOCKED, "warning"),
        (IntegrationStatus.EXECUTION_RETRY_EXHAUSTED, "warning"),
        (IntegrationStatus.FAILURE, "failed"),
    ],
)
def test_pipeline_maps_non_success_step_status_to_progress_event(
    status: IntegrationStatus, expected_event: str
) -> None:
    progress = _RecordingProgress()
    ctx = _make_context(_make_config(progress=progress))

    result = IntegrationPipeline([_StatusStep(status), _BetaStep()]).execute(ctx)

    assert result["status"] == status
    assert progress.events == [
        ("_StatusStep", "started", None, None),
        ("_StatusStep", expected_event, None, status.value),
    ]


def test_pipeline_emits_failed_when_step_aborts() -> None:
    progress = _RecordingProgress()
    ctx = _make_context(_make_config(progress=progress))

    result = IntegrationPipeline([_AbortStep(), _BetaStep()]).execute(ctx)

    assert result["status"] == IntegrationStatus.EXECUTION_INDETERMINATE
    assert progress.events == [
        ("_AbortStep", "started", None, None),
        (
            "_AbortStep",
            "failed",
            None,
            IntegrationStatus.EXECUTION_INDETERMINATE.value,
        ),
    ]


def test_pipeline_emits_failed_when_step_raises_unexpected_error() -> None:
    progress = _RecordingProgress()
    ctx = _make_context(_make_config(progress=progress))

    with pytest.raises(RuntimeError, match="boom"):
        IntegrationPipeline([_RaiseStep()]).execute(ctx)

    assert progress.events == [
        ("_RaiseStep", "started", None, None),
        ("_RaiseStep", "failed", None, "RuntimeError"),
    ]


def test_build_integrator_config_forwards_dispatcher_progress(tmp_path: Path) -> None:
    progress = _RecordingProgress()
    dispatcher_config = DispatcherConfig(
        parent_issue_number=100,
        forge=MagicMock(spec=Forge),
        events_log_path=tmp_path / "events.jsonl",
        progress=progress,
    )

    integrator_config = _build_integrator_config(
        dispatcher_config, semantic_review_enabled=False
    )

    assert integrator_config.progress is progress


def test_multi_issue_integrator_preserves_un_deepcopyable_progress() -> None:
    class _UnDeepcopyableProgress(StdoutProgress):
        def __deepcopy__(self, memo: dict[int, object]) -> _UnDeepcopyableProgress:
            raise TypeError("cannot deepcopy stream progress")

    stream = io.StringIO()
    progress = _UnDeepcopyableProgress("run-1", 100, False, stream=stream)
    ctx = _make_context(_make_config(progress=progress))

    class _ChildIntegrator(IntegrationComponent):
        def __init__(self, parent_issue: int) -> None:
            self.parent_issue = parent_issue

        def execute(self, sub_ctx: IntegrationContext) -> IntegrationReport:
            assert sub_ctx.config.progress is progress
            return {"status": IntegrationStatus.SUCCESS}

    result = MultiIssueIntegrator([_ChildIntegrator(101)]).execute(ctx)

    assert result["status"] == IntegrationStatus.COMPOSITE_SUCCESS
    assert ctx.config.progress is progress


def test_rich_dispatch_progress_nests_pipeline_steps_cleanly() -> None:
    stream = io.StringIO()
    progress = RichDispatchProgress(
        "run-1",
        100,
        False,
        stream=stream,
        env={"NO_COLOR": "1"},
        width=120,
    )
    ctx = _make_context(_make_config(progress=progress))

    progress.emit("run_semantic_integrator", "started")
    IntegrationPipeline([_AlphaStep(), _BetaStep()]).execute(ctx)
    progress.emit("run_semantic_integrator", "completed")
    progress.close()

    output = stream.getvalue()
    assert "_AlphaStep completed" in output
    assert "_BetaStep completed" in output
    assert "run_semantic_integrator completed" in output
    assert not progress._open
