"""Pre-scheduling integration boundary for the Dispatcher (#1377).

Orchestrates pending not-needed review polling, layer-2 semantic review advancement,
and Integrator execution between the pre-integration cycle and the scheduling cycle,
outside the run_state lock.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from pathlib import Path

from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.postcycle_semantic_review import (
    SUCCESS_STATUSES as SEMANTIC_SUCCESS_STATUSES,
)
from orchestune.dispatch.postcycle_semantic_review import (
    WARNING_STATUSES,
    advance_pending_semantic_reviews,
)
from orchestune.dispatch.result import PhaseResult, PhaseStatus
from orchestune.dispatch.targets import ClaudeCodeCloudRoutineDispatchTarget
from orchestune.forge import Forge, ForgeAuthError
from orchestune.integrator import IntegrationStatus, Integrator, IntegratorConfig
from orchestune.integrator.coordinator import (
    DEFAULT_NOT_NEEDED_REVIEW_TIMEOUT_SECONDS,
    IntegrationCoordinator,
    process_pending_not_needed_reviews,
)

_INTEGRATOR_SUCCESS_STATUSES = {
    IntegrationStatus.SUCCESS,
    IntegrationStatus.NO_DONE_TASKS,
    IntegrationStatus.SEMANTIC_REVIEW_PENDING,
}

_INTEGRATOR_HUMAN_REVIEW_STATUSES = {
    IntegrationStatus.EXECUTION_CLEANUP_FAILED,
    IntegrationStatus.EXECUTION_RETRY_EXHAUSTED,
    IntegrationStatus.EXECUTION_INDETERMINATE,
}


def _decide_semantic_review_enabled() -> bool:
    return os.environ.get("ORCHESTUNE_SEMANTIC_REVIEW", "1") != "0"


def _run_best_effort_phase(
    *,
    phase_name: str,
    report_label: str,
    work: Callable[[], dict],
    auth_error: ForgeAuthError | None,
    auth_error_message: str,
    failure_message: str,
    evaluate_report: Callable[[dict], tuple[PhaseStatus, bool]] | None = None,
) -> PhaseResult:
    try:
        if auth_error is not None:
            raise auth_error

        report = work()
        print(f"{report_label}:", file=sys.stderr)
        print(json.dumps(report, ensure_ascii=False, indent=2), file=sys.stderr)

        if evaluate_report is not None:
            status, retryable = evaluate_report(report)
        else:
            status, retryable = PhaseStatus.SUCCESS, False

        return PhaseResult(
            phase_name=phase_name,
            status=status,
            report=report,
            retryable=retryable,
            stage="integration_boundary",
        )
    except ForgeAuthError as e:
        print(f"Error: {auth_error_message}: {e}", file=sys.stderr)
        return PhaseResult(
            phase_name=phase_name,
            status=PhaseStatus.FATAL_FAILURE,
            error_message=str(e),
            retryable=False,
            stage="integration_boundary",
        )
    except Exception as e:
        print(f"Warning: {failure_message}: {e}", file=sys.stderr)
        return PhaseResult(
            phase_name=phase_name,
            status=PhaseStatus.RETRYABLE_FAILURE,
            error_message=str(e),
            retryable=True,
            stage="integration_boundary",
        )


def _poll_pending_not_needed_reviews(
    state_path: Path,
    forge: Forge | None = None,
    auth_error: ForgeAuthError | None = None,
    timeout_seconds: float = DEFAULT_NOT_NEEDED_REVIEW_TIMEOUT_SECONDS,
) -> PhaseResult:
    def work() -> dict:
        return process_pending_not_needed_reviews(
            state_path, forge=forge, timeout_seconds=timeout_seconds
        )

    return _run_best_effort_phase(
        phase_name="poll_pending_not_needed_reviews",
        report_label="Pending Not-Needed Review Report",
        work=work,
        auth_error=auth_error,
        auth_error_message="authentication failed while polling reviews",
        failure_message="failed to process pending not-needed reviews",
    )


def _build_integrator_config(
    config: DispatcherConfig, semantic_review_enabled: bool
) -> IntegratorConfig:
    integrator_config = IntegratorConfig(
        parent_issue_number=config.parent_issue_number,
        apply=config.apply,
        forge=config.forge,
        ci_command=config.ci_command,
        dag_ignore_patterns=config.dag_ignore_patterns,
        dag_similarity_threshold=config.dag_similarity_threshold,
        child_review_gate=config.child_review_gate,
        semantic_review_mode=config.semantic_review_mode,
        force_integration_review=config.force_integration_review,
        integration_dependency_timeout_seconds=config.integration_dependency_timeout_seconds,
        integration_ci_timeout_seconds=config.integration_ci_timeout_seconds,
        integration_cycle_timeout_seconds=config.integration_cycle_timeout_seconds,
        integration_cleanup_timeout_seconds=config.integration_cleanup_timeout_seconds,
        integration_command_timeout_seconds=config.integration_command_timeout_seconds,
        max_integration_timeout_retries=config.max_integration_timeout_retries,
        integration_timeout_backoff_seconds=config.integration_timeout_backoff_seconds,
    )
    if config.semantic_review_mode == "required":
        integrator_config.enable_semantic_review = True
    elif config.semantic_review_mode == "advisory":
        if semantic_review_enabled and isinstance(
            config.dispatch_target, ClaudeCodeCloudRoutineDispatchTarget
        ):
            integrator_config.enable_semantic_review = True
            integrator_config.coordinator = IntegrationCoordinator(
                config.dispatch_target
            )
        else:
            integrator_config.enable_semantic_review = False
    else:
        integrator_config.enable_semantic_review = False
    return integrator_config


def _advance_pending_semantic_reviews(
    config: DispatcherConfig, auth_error: ForgeAuthError | None = None
) -> PhaseResult:
    def work() -> dict:
        return advance_pending_semantic_reviews(config)

    def evaluate_report(report: dict) -> tuple[PhaseStatus, bool]:
        statuses = {attempt.get("status") for attempt in report.get("attempts", [])}
        if statuses - SEMANTIC_SUCCESS_STATUSES - WARNING_STATUSES:
            return PhaseStatus.RETRYABLE_FAILURE, True
        cleanup = {entry.get("status") for entry in report.get("cleanup", [])}
        if statuses & WARNING_STATUSES or "failed" in cleanup:
            return PhaseStatus.WARNING, False
        return PhaseStatus.SUCCESS, False

    return _run_best_effort_phase(
        phase_name="advance_pending_semantic_reviews",
        report_label="Semantic Review Cycle Report",
        work=work,
        auth_error=auth_error,
        auth_error_message="authentication failed while advancing semantic reviews",
        failure_message="failed to advance pending semantic reviews",
        evaluate_report=evaluate_report,
    )


def _run_semantic_integrator(
    config: DispatcherConfig,
    semantic_review_enabled: bool,
    auth_error: ForgeAuthError | None = None,
) -> PhaseResult:
    def work() -> dict:
        integrator_config = _build_integrator_config(config, semantic_review_enabled)
        return dict(Integrator(integrator_config).run())

    def evaluate_report(report: dict) -> tuple[PhaseStatus, bool]:
        status = report.get("status")
        if status == IntegrationStatus.REVIEW_GATE_BLOCKED:
            return PhaseStatus.WARNING, False
        if status in _INTEGRATOR_HUMAN_REVIEW_STATUSES:
            return PhaseStatus.WARNING, False
        if status not in _INTEGRATOR_SUCCESS_STATUSES:
            return PhaseStatus.RETRYABLE_FAILURE, True
        return PhaseStatus.SUCCESS, False

    return _run_best_effort_phase(
        phase_name="run_semantic_integrator",
        report_label="Integrator Report",
        work=work,
        auth_error=auth_error,
        auth_error_message="authentication failed while running Integrator",
        failure_message="Integrator failed to run",
        evaluate_report=evaluate_report,
    )


def _pre_scheduling_integration_steps(
    config: DispatcherConfig,
    semantic_review_enabled: bool,
    auth_error: ForgeAuthError | None = None,
) -> list[tuple[str, Callable[[], PhaseResult], bool]]:
    return [
        (
            "poll_pending_not_needed_reviews",
            lambda: _poll_pending_not_needed_reviews(
                config.not_needed_review_state_path,
                forge=config.forge,
                auth_error=auth_error,
                timeout_seconds=config.not_needed_review_timeout_seconds,
            ),
            semantic_review_enabled,
        ),
        (
            "advance_pending_semantic_reviews",
            lambda: _advance_pending_semantic_reviews(config, auth_error=auth_error),
            True,
        ),
        (
            "run_semantic_integrator",
            lambda: _run_semantic_integrator(
                config,
                semantic_review_enabled,
                auth_error=auth_error,
            ),
            True,
        ),
    ]
