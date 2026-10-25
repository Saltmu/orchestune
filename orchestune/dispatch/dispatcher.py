from __future__ import annotations

import argparse
import os
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NoReturn

from orchestune.claim.workspace import resolve_claim_workspace
from orchestune.config_contracts import ConfigError
from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.config_loader import (
    build_arg_parser as build_arg_parser,
)
from orchestune.dispatch.config_loader import (
    load_and_resolve_config as load_and_resolve_config,
)
from orchestune.dispatch.config_schema import validate_toml_config
from orchestune.dispatch.cycle import (
    run_dispatch_cycle,
    run_pre_integration_cycle,
)
from orchestune.dispatch.cycle_report import CycleReport
from orchestune.dispatch.postcycle import (
    _post_event_log_comment,
    _post_finding_notices,
    _process_parent_completion,
)
from orchestune.dispatch.pre_scheduling_integration import (
    _advance_pending_semantic_reviews as _advance_pending_semantic_reviews,
)
from orchestune.dispatch.pre_scheduling_integration import (
    _build_integrator_config as _build_integrator_config,
)
from orchestune.dispatch.pre_scheduling_integration import (
    _decide_semantic_review_enabled as _decide_semantic_review_enabled,
)
from orchestune.dispatch.pre_scheduling_integration import (
    _poll_pending_not_needed_reviews as _poll_pending_not_needed_reviews,
)
from orchestune.dispatch.pre_scheduling_integration import (
    _run_semantic_integrator as _run_semantic_integrator,
)
from orchestune.dispatch.progress import StdoutProgress, progress_phase, safe_stderr
from orchestune.dispatch.report import _report_to_dict, write_github_step_summary
from orchestune.dispatch.report_output import ReportOutput, new_run_id, reserve_report
from orchestune.dispatch.result import PhaseResult, PhaseStatus
from orchestune.dispatch.summary import (
    merge_skips,
    render_forge_warnings_text,
    render_skipped_text,
)
from orchestune.dispatch.targets import (
    build_dispatch_target as build_dispatch_target,  # compatibility patch surface
)
from orchestune.forge import ForgeAuthError
from orchestune.infra.repository_config import find_and_load_config_file

_build_arg_parser = build_arg_parser
load_config_file = find_and_load_config_file


def _config_error(parser: argparse.ArgumentParser, message: str) -> NoReturn:
    parser.error(f"invalid dispatcher config: {message}")


def _config_defaults(
    parser: argparse.ArgumentParser, config_data: dict[str, Any]
) -> dict[str, Any]:
    """Validate TOML values before using them."""
    try:
        validated = validate_toml_config(config_data)
        destinations = {action.dest for action in parser._actions}  # noqa: SLF001 - argparse private access
        return {k: v for k, v in validated.items() if k in destinations}
    except ConfigError as e:
        _config_error(parser, str(e))


@dataclass(frozen=True)
class _DispatcherRunResult:
    report: Any
    post_cycle_results: list[PhaseResult]
    integrator_run_report: Any
    pre_integration_report: Any | None = None
    integration_results: list[PhaseResult] = field(default_factory=list)
    scheduling_deferred_reason: str | None = None
    lock_contended: bool = False


def _resolve_dispatch_shared_paths(
    args: Any,
    cwd: Path | None,
) -> tuple[Path, Path]:
    """Resolve claim-shared paths against the primary checkout root (#966)."""
    workspace = resolve_claim_workspace(
        cwd,
        explicit_state_path=getattr(args, "run_state_path", None),
        explicit_worktree_root=getattr(args, "worktree_root", None),
    )
    return workspace.run_state_path, workspace.worktree_root


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


def _post_cycle_steps(
    config: DispatcherConfig,
    report: Any,
    semantic_review_enabled: bool,
    auth_error: ForgeAuthError | None,
    pre_integration_report: Any | None = None,
) -> list[tuple[str, Callable[[], PhaseResult], bool]]:
    return [
        (
            "process_parent_completion",
            lambda: _process_parent_completion(config, auth_error=auth_error),
            True,
        ),
        (
            "post_event_log_comment",
            lambda: _post_event_log_comment(
                config,
                report,
                auth_error=auth_error,
                pre_integration_report=pre_integration_report,
            ),
            True,
        ),
        (
            "post_finding_notices",
            lambda: _post_finding_notices(
                config,
                report,
                auth_error=auth_error,
                pre_integration_report=pre_integration_report,
            ),
            True,
        ),
    ]


def _run_post_cycle_step(
    config: DispatcherConfig, name: str, call: Callable[[], PhaseResult]
) -> tuple[PhaseResult, bool]:
    config.progress.emit(name, "started")
    fatal_exception = False
    try:
        result = call()
    except Exception as exc:
        result = PhaseResult(name, PhaseStatus.FATAL_FAILURE, error_message=str(exc))
        fatal_exception = True
    event = {
        PhaseStatus.SUCCESS: "completed",
        PhaseStatus.WARNING: "warning",
        PhaseStatus.RETRYABLE_FAILURE: "failed",
        PhaseStatus.FATAL_FAILURE: "failed",
    }[result.status]
    config.progress.emit(name, event)
    if result.error_message:
        safe_stderr(f"{name}: {result.status.value}: {result.error_message}")
    return result, fatal_exception


def _run_integration_boundary(
    config: DispatcherConfig, decide_review_fn: Callable[[], bool]
) -> tuple[list[PhaseResult], Any | None, bool, ForgeAuthError | None, bool]:
    auth_error = None
    semantic_review_enabled = False
    preparation_failed = False
    integration_results: list[PhaseResult] = []
    integrator_report = None

    try:
        with progress_phase(config.progress, "integration_preparation"):
            try:
                config.resolved_forge.check_auth()
            except ForgeAuthError as exc:
                auth_error = exc
            semantic_review_enabled = decide_review_fn()
    except Exception as exc:
        integration_results.append(
            PhaseResult(
                "integration_preparation",
                PhaseStatus.FATAL_FAILURE,
                error_message=str(exc),
                stage="integration_boundary",
            )
        )
        safe_stderr(f"integration_preparation: {exc}")
        preparation_failed = True

    stopped = preparation_failed
    for name, call, enabled in _pre_scheduling_integration_steps(
        config, semantic_review_enabled, auth_error
    ):
        if stopped or not enabled:
            reason = "prior_failure" if stopped else "semantic_review_disabled"
            config.progress.emit(name, "skipped", reason=reason)
            continue
        result, fatal = _run_post_cycle_step(config, name, call)
        integration_results.append(result)
        if fatal:
            stopped = True
        if name == "run_semantic_integrator":
            integrator_report = result.report

    return (
        integration_results,
        integrator_report,
        semantic_review_enabled,
        auth_error,
        stopped,
    )


def _run_scheduling_cycle_stage(
    config: DispatcherConfig,
    run_dispatch_fn: Callable[[DispatcherConfig], CycleReport],
    pre_integration_report: CycleReport,
    stopped: bool,
    auth_error: ForgeAuthError | None,
) -> tuple[CycleReport, str | None, bool]:
    if stopped or auth_error is not None:
        config.progress.emit("cycle", "skipped", reason="prior_failure")
        return pre_integration_report, "prior_failure", False
    try:
        return run_dispatch_fn(config), None, False
    except RuntimeError as exc:
        err_msg = str(exc)
        if "holding the lock" in err_msg or "is currently held" in err_msg:
            config.progress.emit("cycle", "skipped", reason="state_lock_contended")
            return pre_integration_report, "state_lock_contended", True
        raise


def _run_post_cycle_phases(
    config: DispatcherConfig,
    scheduling_report: Any,
    semantic_review_enabled: bool,
    auth_error: ForgeAuthError | None,
    pre_integration_report: Any,
    post_stopped: bool,
) -> list[PhaseResult]:
    post_results: list[PhaseResult] = []
    for name, call, enabled in _post_cycle_steps(
        config,
        scheduling_report,
        semantic_review_enabled,
        auth_error,
        pre_integration_report=pre_integration_report,
    ):
        if post_stopped or not enabled:
            reason = "prior_failure" if post_stopped else "disabled"
            config.progress.emit(name, "skipped", reason=reason)
            continue
        result, fatal = _run_post_cycle_step(config, name, call)
        post_results.append(result)
        if fatal:
            post_stopped = True
    return post_results


def _run_dispatcher(config: DispatcherConfig) -> _DispatcherRunResult:
    if not config.apply:
        report = run_dispatch_cycle(config)
        for name, _call, _enabled in _post_cycle_steps(config, report, False, None):
            config.progress.emit(name, "skipped", reason="dry_run")
        return _DispatcherRunResult(
            report=report, post_cycle_results=[], integrator_run_report=None
        )

    pre_integration_report = run_pre_integration_cycle(config)
    (
        integration_results,
        integrator_report,
        semantic_review_enabled,
        auth_error,
        stopped,
    ) = _run_integration_boundary(config, _decide_semantic_review_enabled)

    scheduling_report, deferred_reason, lock_contended = _run_scheduling_cycle_stage(
        config, run_dispatch_cycle, pre_integration_report, stopped, auth_error
    )

    post_results = _run_post_cycle_phases(
        config,
        scheduling_report,
        semantic_review_enabled,
        auth_error,
        pre_integration_report,
        post_stopped=stopped,
    )

    return _DispatcherRunResult(
        report=scheduling_report,
        post_cycle_results=[*integration_results, *post_results],
        integrator_run_report=integrator_report,
        pre_integration_report=pre_integration_report,
        integration_results=integration_results,
        scheduling_deferred_reason=deferred_reason,
        lock_contended=lock_contended,
    )


def _emit_human_summary(report: CycleReport) -> None:
    """#787: 未選定タスクとForge障害の要約をstderrへ出す。

    stdoutの進捗と結果JSONのファイル保存とは独立したbest-effort経路。

    整形の失敗でサイクルの結果報告を落とさないよう、ベストエフォートで囲む
    （`write_github_step_summary`と同じ方針）。stderrがcp932のコンソールへ
    繋がる場合に備え、`summary`側のテキスト経路はASCIIのみで組んである。
    """
    try:
        lines = render_skipped_text(
            merge_skips(report.skips, report.scheduling_decisions)
        ) + render_forge_warnings_text(report.forge_warnings)
        for line in lines:
            safe_stderr(line)
    except Exception as e:  # noqa: BLE001 - 要約はベストエフォート
        safe_stderr(f"Warning: Failed to render the cycle summary: {e}")


def _emit_dispatcher_report(result: _DispatcherRunResult) -> None:
    _emit_human_summary(result.report)

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        write_github_step_summary(
            cycle_report=result.report,
            integrator_report=result.integrator_run_report,
            summary_path=summary_path,
            post_cycle_results=result.post_cycle_results,
            pre_integration_report=result.pre_integration_report,
        )


def _post_cycle_exit_code(
    results: list[PhaseResult], lock_contended: bool = False
) -> int:
    exit_code = 2 if lock_contended else 0
    for result in results:
        if result.status == PhaseStatus.FATAL_FAILURE:
            return 1
        if result.status == PhaseStatus.RETRYABLE_FAILURE and exit_code != 1:
            exit_code = 2
    return exit_code


def main(argv: list[str] | None = None, cwd: Path | None = None) -> int:
    sink = StdoutProgress(new_run_id(), None, None)
    try:
        return _main_with_sink(argv, cwd, sink)
    finally:
        sink.close()


def _main_with_sink(
    argv: list[str] | None, cwd: Path | None, sink: StdoutProgress
) -> int:
    parser = _build_arg_parser()
    sink.emit("configuration", "started")
    try:
        config = load_and_resolve_config(
            argv,
            cwd,
            load_config_fn=load_config_file,
            build_target_fn=build_dispatch_target,
            resolve_paths_fn=_resolve_dispatch_shared_paths,
        )
    except (ConfigError, ValueError) as e:
        sink.emit("configuration", "failed")
        _config_error(parser, str(e))

    sink.parent_issue = config.parent_issue_number
    sink.apply = config.apply
    sink.emit("configuration", "completed")
    config.progress = sink
    try:
        with ExitStack() as stack:
            with progress_phase(sink, "report_reservation"):
                output = stack.enter_context(reserve_report(config, sink.run_id))
            sink.emit("report", "planned", reason=f"report target: {output.path}")
            return _execute_and_save(config, output)
    except KeyboardInterrupt:
        sink.emit("execution", "failed", reason="interrupted; report not created")
        raise
    except Exception as exc:
        safe_stderr(f"Error: {exc}")
        return 1


def _execute_and_save(config: DispatcherConfig, output: ReportOutput) -> int:
    try:
        result = _run_dispatcher(config)
    except Exception as exc:
        config.progress.emit(
            "report", "skipped", reason=f"report not created: {output.path}"
        )
        safe_stderr(f"Error: {exc}; report not created: {output.path}")
        return 1
    code = _post_cycle_exit_code(
        result.post_cycle_results, lock_contended=result.lock_contended
    )
    try:
        with progress_phase(config.progress, "report_save"):
            final_dict = _report_to_dict(result.report)
            if result.pre_integration_report is not None:
                final_dict["pre_integration_cycle"] = _report_to_dict(
                    result.pre_integration_report
                )
            if result.scheduling_deferred_reason is not None:
                final_dict["scheduling_deferred_reason"] = (
                    result.scheduling_deferred_reason
                )
            final_dict["post_cycle_results"] = [
                phase.to_dict() for phase in result.post_cycle_results
            ]
            output.save(final_dict)
        config.progress.emit(
            "report", "completed", reason=f"report saved: {output.path}"
        )
    except Exception as exc:
        safe_stderr(f"report save failed: {output.path}: {exc}")
        code = 1
    try:
        _emit_dispatcher_report(result)
    except Exception as exc:
        safe_stderr(f"Warning: summary unavailable: {exc}")
    config.progress.emit(
        "execution", "completed" if code == 0 else "failed", reason=f"exit_code={code}"
    )
    return code


if __name__ == "__main__":
    raise SystemExit(main())
