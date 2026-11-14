from __future__ import annotations

import argparse
import os
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn

from orchestune.claim.workspace import resolve_checkout_roots, resolve_claim_workspace
from orchestune.config_contracts import ConfigError
from orchestune.dispatch import pre_scheduling_integration
from orchestune.dispatch.config.loader import (
    build_arg_parser as build_arg_parser,
)
from orchestune.dispatch.config.loader import (
    load_and_resolve_config as load_and_resolve_config,
)
from orchestune.dispatch.config.model import DispatcherConfig
from orchestune.dispatch.config.schema import validate_toml_config
from orchestune.dispatch.cycle.report import CycleReport
from orchestune.dispatch.cycle.run import (
    run_dispatch_cycle,
    run_pre_integration_cycle,
)
from orchestune.dispatch.postcycle import (
    _post_event_log_comment,
    _post_finding_notices,
    _process_parent_completion,
)
from orchestune.dispatch.pre_scheduling_integration import (
    _decide_semantic_review_enabled as _decide_semantic_review_enabled,
)
from orchestune.dispatch.reporting.progress import (
    DispatchProgressSink,
    progress_phase,
    safe_stderr,
)
from orchestune.dispatch.reporting.progress_output import select_progress_sink
from orchestune.dispatch.reporting.report import (
    _report_to_dict,
    write_github_step_summary,
)
from orchestune.dispatch.reporting.report_output import (
    ReportOutput,
    new_run_id,
    reserve_report,
)
from orchestune.dispatch.reporting.summary import (
    merge_skips,
    render_forge_warnings_text,
    render_skipped_text,
)
from orchestune.dispatch.result import PhaseResult, PhaseStatus
from orchestune.dispatch.targets import (
    build_dispatch_target as build_dispatch_target,  # compatibility patch surface
)
from orchestune.forge import ForgeAuthError
from orchestune.infra.process_utils import platform_ci_script_relative
from orchestune.infra.repository_config import find_and_load_config_file
from orchestune.integrator.types import CHILD_REVIEW_REPORT_REASONS
from orchestune.status_snapshot import observe_repository_id

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
    except Exception as exc:  # noqa: BLE001 - boundary: normalize phase, execution, and report callback failures into existing status/exit results
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


def _prepare_integration_boundary(
    config: DispatcherConfig, decide_review_fn: Callable[[], bool]
) -> tuple[bool, ForgeAuthError | None, PhaseResult | None]:
    auth_error = None
    try:
        with progress_phase(config.progress, "integration_preparation"):
            try:
                config.resolved_forge.check_auth()
            except ForgeAuthError as exc:
                auth_error = exc
            semantic_review_enabled = decide_review_fn()
        return semantic_review_enabled, auth_error, None
    except Exception as exc:  # noqa: BLE001 - boundary: normalize phase, execution, and report callback failures into existing status/exit results
        failure = PhaseResult(
            "integration_preparation",
            PhaseStatus.FATAL_FAILURE,
            error_message=str(exc),
            stage="integration_boundary",
        )
        safe_stderr(f"integration_preparation: {exc}")
        return False, None, failure


def _run_integration_boundary(
    config: DispatcherConfig, decide_review_fn: Callable[[], bool]
) -> tuple[list[PhaseResult], Any | None, bool, ForgeAuthError | None, bool]:
    semantic_review_enabled, auth_error, prep_failure = _prepare_integration_boundary(
        config, decide_review_fn
    )
    integration_results: list[PhaseResult] = (
        [prep_failure] if prep_failure is not None else []
    )
    integrator_report = None
    stopped = prep_failure is not None

    for (
        name,
        call,
        enabled,
    ) in pre_scheduling_integration._pre_scheduling_integration_steps(  # noqa: SLF001 - attribute access preserves test patch targets
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
    sink = select_progress_sink(new_run_id())
    try:
        return _main_with_sink(argv, cwd, sink)
    finally:
        sink.close()


def _main_with_sink(
    argv: list[str] | None, cwd: Path | None, sink: DispatchProgressSink
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
    if config.apply and config.ci_command is None:
        _, checkout = resolve_checkout_roots(cwd or Path.cwd())
        if not (checkout / platform_ci_script_relative()).is_file():
            _config_error(
                parser,
                'Set ci-command = "pytest" in orchestune.toml or [tool.orchestune] in pyproject.toml; the default CI script is missing. There is no --ci-command CLI option.',
            )
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
    except Exception as exc:  # noqa: BLE001 - boundary: normalize phase, execution, and report callback failures into existing status/exit results
        safe_stderr(f"Error: {exc}")
        return 1


_COMPOSITE_STATUSES = frozenset(
    {"composite_success", "composite_partial_success", "composite_failure"}
)
_EXECUTION_FIELDS = (
    "cause",
    "stage",
    "attempt",
    "max_attempts",
    "next_retry_at",
    "stop_confirmed",
    "rollback_confirmed",
    "side_effect_state",
)
_TARGET_FIELDS = ("issue_number", "subtask_id", "source_sha")


def _utc_now() -> datetime:
    return datetime.now(UTC)


def build_parent_integration(
    *,
    repository_id: str | None,
    parent_issue: int | None,
    observed_at: str,
    apply: bool,
    integrator_report: Any,
) -> dict[str, Any]:
    """One parent observation. Missing integration is not stored as success."""
    selected, executed = _select_parent_report(integrator_report, parent_issue)
    status = _integration_status(selected) if executed else None
    failures = _project_saved_failures(selected) if executed and selected else []
    return {
        "version": 1,
        "repository_id": repository_id,
        "parent_issue": parent_issue,
        "observed_at": observed_at,
        "mode": "apply" if apply else "preview",
        "executed": executed,
        "status": status,
        "failures": failures,
    }


def _select_parent_report(
    report: Any, parent_issue: int | None
) -> tuple[dict[str, Any] | None, bool]:
    if not isinstance(report, dict):
        return None, False
    status = _status_text(report.get("status"))
    details = report.get("details")
    if status not in _COMPOSITE_STATUSES:
        return report, True
    if not isinstance(parent_issue, int) or isinstance(parent_issue, bool):
        return None, False
    if not isinstance(details, dict):
        return None, False
    chosen = details.get(f"issue_{parent_issue}")
    if not isinstance(chosen, dict):
        return None, False
    return chosen, True


def _integration_status(report: dict[str, Any] | None) -> str | None:
    if report is None:
        return None
    return _status_text(report.get("status"))


def _status_text(status: Any) -> str | None:
    value = getattr(status, "value", status)
    if isinstance(value, str) and value:
        return value
    return None


def _project_saved_failures(report: dict[str, Any]) -> list[dict[str, Any]]:
    items = [
        _child_review_item(report, raw)
        for raw in _dict_list(report, "child_review_gate_failures")
    ]
    items.extend(
        _execution_item(raw) for raw in _dict_list(report, "execution_failures")
    )
    return [item for item in items if item]


def _dict_list(report: dict[str, Any], key: str) -> list[dict[str, Any]]:
    raw = report.get(key)
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def _child_review_item(report: dict[str, Any], raw: dict[str, Any]) -> dict[str, Any]:
    reason = raw.get("reason")
    if reason not in CHILD_REVIEW_REPORT_REASONS:
        return {}
    item: dict[str, Any] = {"kind": "child_review_gate", "reason": reason}
    number = _positive_int(raw.get("issue_number"))
    if number is not None:
        item["issue_number"] = number
    sha = _plain_token(raw.get("expected_sha"))
    if sha is not None:
        item["expected_sha"] = sha
    pr_number = _positive_int(report.get("integration_pr_number"))
    if pr_number is not None:
        item["integration_pr_number"] = pr_number
    return item


def _execution_item(raw: dict[str, Any]) -> dict[str, Any]:
    item: dict[str, Any] = {"kind": "execution"}
    for key in _EXECUTION_FIELDS:
        if key not in raw:
            continue
        value = raw[key]
        if (
            isinstance(value, bool)
            or value is None
            or _plain_token(value)
            or _whole_number(value)
        ):
            item[key] = value
    target = _execution_target(raw)
    if target:
        item["target"] = target
    return item


def _execution_target(raw: dict[str, Any]) -> dict[str, Any]:
    target: dict[str, Any] = {}
    number = _positive_int(raw.get("issue_number"))
    if number is not None:
        target["issue_number"] = number
    for key in _TARGET_FIELDS[1:]:
        token = _plain_token(raw.get(key))
        if token is not None:
            target[key] = token
    return target


def _positive_int(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def _whole_number(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _plain_token(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    if "/" in value or "\\" in value or value.startswith("."):
        return None
    return value


def _execute_and_save(config: DispatcherConfig, output: ReportOutput) -> int:
    try:
        result = _run_dispatcher(config)
    except Exception as exc:  # noqa: BLE001 - boundary: normalize phase, execution, and report callback failures into existing status/exit results
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
            final_dict["parent_integration"] = build_parent_integration(
                repository_id=observe_repository_id(Path.cwd(), config.resolved_forge),
                parent_issue=config.parent_issue_number,
                observed_at=_utc_now().strftime("%Y-%m-%dT%H:%M:%SZ"),
                apply=config.apply,
                integrator_report=result.integrator_run_report,
            )
            output.save(final_dict)
        config.progress.emit(
            "report", "completed", reason=f"report saved: {output.path}"
        )
    except Exception as exc:  # noqa: BLE001 - boundary: normalize phase, execution, and report callback failures into existing status/exit results
        safe_stderr(f"report save failed: {output.path}: {exc}")
        code = 1
    try:
        _emit_dispatcher_report(result)
    except Exception as exc:  # noqa: BLE001 - boundary: normalize phase, execution, and report callback failures into existing status/exit results
        safe_stderr(f"Warning: summary unavailable: {exc}")
    config.progress.emit(
        "execution", "completed" if code == 0 else "failed", reason=f"exit_code={code}"
    )
    return code


if __name__ == "__main__":
    raise SystemExit(main())
