from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

from orchestune.claim.workspace import resolve_claim_workspace
from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.config_loader import (
    ConfigError as ConfigError,
)
from orchestune.dispatch.config_loader import (
    build_arg_parser as build_arg_parser,
)
from orchestune.dispatch.config_loader import (
    find_and_load_config_file as find_and_load_config_file,
)
from orchestune.dispatch.config_loader import (
    load_and_resolve_config as load_and_resolve_config,
)
from orchestune.dispatch.config_loader import (
    validate_toml_config as validate_toml_config,
)
from orchestune.dispatch.cycle import run_dispatch_cycle
from orchestune.dispatch.cycle_report import CycleReport
from orchestune.dispatch.execution_profiles import ExecutionProfileConfig
from orchestune.dispatch.postcycle import (
    _decide_semantic_review_enabled,
    _poll_pending_not_needed_reviews,
    _post_event_log_comment,
    _post_finding_notices,
    _process_parent_completion,
    _run_semantic_integrator,
)
from orchestune.dispatch.report import _report_to_dict, write_github_step_summary
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
class _DispatcherInputs:
    args: argparse.Namespace
    dag_ignore_patterns: tuple[re.Pattern[str], ...]
    dag_similarity_threshold: float
    execution_profile_config: ExecutionProfileConfig | None
    run_state_path: Path
    worktree_root: Path


@dataclass(frozen=True)
class _DispatcherRunResult:
    report: Any
    post_cycle_results: list[PhaseResult]
    integrator_run_report: Any


def _load_dispatcher_inputs(
    parser: argparse.ArgumentParser,
    argv: list[str] | None,
    cwd: Path | None,
) -> _DispatcherInputs:
    try:
        cfg = load_and_resolve_config(
            argv,
            cwd,
            load_config_fn=load_config_file,
            build_target_fn=build_dispatch_target,
            resolve_paths_fn=_resolve_dispatch_shared_paths,
        )
    except (ConfigError, ValueError) as e:
        _config_error(parser, str(e))

    args = parser.parse_args(argv)
    return _DispatcherInputs(
        args=args,
        dag_ignore_patterns=cfg.dag_ignore_patterns,
        dag_similarity_threshold=cfg.dag_similarity_threshold,
        execution_profile_config=cfg.execution_profile_config,
        run_state_path=cfg.run_state_path,
        worktree_root=cfg.worktree_root,
    )


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


def _run_dispatcher(config: DispatcherConfig) -> _DispatcherRunResult:
    report = run_dispatch_cycle(config)
    post_cycle_results: list[PhaseResult] = []
    integrator_run_report = None

    if config.apply:
        auth_error = None
        try:
            config.resolved_forge.check_auth()
        except ForgeAuthError as e:
            auth_error = e

        semantic_review_enabled = _decide_semantic_review_enabled()
        if semantic_review_enabled:
            result = _poll_pending_not_needed_reviews(
                config.not_needed_review_state_path,
                forge=config.forge,
                auth_error=auth_error,
                timeout_seconds=config.not_needed_review_timeout_seconds,
            )
            post_cycle_results.append(result)
        result = _run_semantic_integrator(
            config, semantic_review_enabled, auth_error=auth_error
        )
        post_cycle_results.append(result)
        integrator_run_report = result.report
        post_cycle_results.append(
            _process_parent_completion(config, auth_error=auth_error)
        )
        post_cycle_results.append(
            _post_event_log_comment(config, report, auth_error=auth_error)
        )
        post_cycle_results.append(
            _post_finding_notices(config, report, auth_error=auth_error)
        )

    return _DispatcherRunResult(
        report=report,
        post_cycle_results=post_cycle_results,
        integrator_run_report=integrator_run_report,
    )


def _emit_human_summary(report: CycleReport) -> None:
    """#787: 未選定タスクとForge障害の要約をstderrへ出す。

    stdoutはスキル・CIがパースするJSON専用の経路なので触らない。Markdownの
    サマリーはGITHUB_STEP_SUMMARYがある実行環境でしか作られず、ローカル実行や
    Codex Cloudでは人間向けの出力が一切無かった。

    整形の失敗でサイクルの結果報告を落とさないよう、ベストエフォートで囲む
    （`write_github_step_summary`と同じ方針）。stderrがcp932のコンソールへ
    繋がる場合に備え、`summary`側のテキスト経路はASCIIのみで組んである。
    """
    try:
        lines = render_skipped_text(
            merge_skips(report.skips, report.scheduling_decisions)
        ) + render_forge_warnings_text(report.forge_warnings)
        for line in lines:
            print(line, file=sys.stderr)
    except Exception as e:  # noqa: BLE001 - 要約はベストエフォート
        print(f"Warning: Failed to render the cycle summary: {e}", file=sys.stderr)


def _emit_dispatcher_report(result: _DispatcherRunResult) -> None:
    final_dict = _report_to_dict(result.report)
    final_dict["post_cycle_results"] = [
        phase.to_dict() for phase in result.post_cycle_results
    ]
    print(json.dumps(final_dict, ensure_ascii=False, indent=2))
    _emit_human_summary(result.report)

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        write_github_step_summary(
            cycle_report=result.report,
            integrator_report=result.integrator_run_report,
            summary_path=summary_path,
            post_cycle_results=result.post_cycle_results,
        )


def _post_cycle_exit_code(results: list[PhaseResult]) -> int:
    exit_code = 0
    for result in results:
        if result.status == PhaseStatus.FATAL_FAILURE:
            exit_code = 1
        elif result.status == PhaseStatus.RETRYABLE_FAILURE and exit_code != 1:
            exit_code = 2
    return exit_code


def main(argv: list[str] | None = None, cwd: Path | None = None) -> int:
    parser = _build_arg_parser()
    try:
        config = load_and_resolve_config(
            argv,
            cwd,
            load_config_fn=load_config_file,
            build_target_fn=build_dispatch_target,
            resolve_paths_fn=_resolve_dispatch_shared_paths,
        )
    except (ConfigError, ValueError) as e:
        _config_error(parser, str(e))

    try:
        result = _run_dispatcher(config)
        _emit_dispatcher_report(result)
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    return _post_cycle_exit_code(result.post_cycle_results)


if __name__ == "__main__":
    raise SystemExit(main())
