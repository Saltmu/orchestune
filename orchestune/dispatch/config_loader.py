"""Configuration loading, schema validation, and precedence resolution for orchestune-dispatch."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import orchestune.claim.workspace as claim_workspace
import orchestune.config_contracts as config_contracts
from orchestune.claim.workspace import resolve_claim_workspace
from orchestune.consistency.supervisor import ConsistencyMode
from orchestune.dag.models import (
    compile_extra_ignore_patterns,
    extract_dag_ignore_patterns,
    extract_dag_similarity_threshold,
)
from orchestune.dag.similarity import DEFAULT_SIMILARITY_THRESHOLD
from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.config_schema import (
    parse_ci_command,
    validate_toml_config,
)
from orchestune.dispatch.config_values import runtime_tuning_overrides
from orchestune.dispatch.execution_profiles import (
    ExecutionProfileConfig,
    extract_execution_profile_config,
    extract_target_name,
    validate_profile_for_target,
)
from orchestune.dispatch.report_output import _check_target
from orchestune.dispatch.targets import (
    ROUTINE_ID_ENV_VAR,
    ROUTINE_TOKEN_ENV_VAR,
    TargetBuildConfig,
    build_dispatch_target,
    resolve_default_dispatch_target_name,
)
from orchestune.infra.git_cli import run_git
from orchestune.infra.repository_config import (
    find_and_load_config_file as find_and_load_config_file,
)
from orchestune.outcome_record import VALID_CHILD_REVIEW_GATE_MODES
from orchestune.review.semantic_gate import VALID_SEMANTIC_REVIEW_MODES

_DISPATCH_TARGET_HELP = (
    "#215/#163: エージェントの実ディスパッチ先。未指定時は実行環境から自動選択される"
    "（GitHub Actions実行時は'cloud-routine'、ローカル実行時は'auto'）。"
    "'auto'はPATH上のローカルCLIを検出し（claude優先、次点agy、codex）、"
    "見つかったCLIへディスパッチする。未検出時は警告を出しダミー起動にフォールバック。"
    "'local'はダミー起動（no-op）。'cloud-routine'はClaude Codeクラウドルーチンへディスパッチ。"
    "'codex-cloud'はCodex Cloud CLIへディスパッチ。"
    "'claude-cli'/'agy-cli'/'codex-cli'はローカルCLIへディスパッチする。"
)


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError(f"must be greater than 0 (got {parsed})")
    return parsed


def _non_negative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError(
            f"0以上の整数を指定してください (got {parsed})"
        )
    return parsed


def _add_core_cli_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "-p",
        "--parent-issue",
        type=_positive_int,
        default=None,
        help="開発対象をまとめている親の GitHub Issue 番号（未指定時は現在ブランチから推論）",
    )
    parser.add_argument(
        "--apply",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="実際にラベル更新・worktree作成・エージェント起動を行う（既定）。"
        "--no-applyでdry-run（dispatch対象へ適用せず、ローカル結果を保存）にできる。",
    )
    parser.add_argument(
        "--dispatch-target",
        choices=[
            "local",
            "cloud-routine",
            "codex-cloud",
            "claude-cli",
            "agy-cli",
            "codex-cli",
            "auto",
        ],
        default=None,
        help=_DISPATCH_TARGET_HELP,
    )
    parser.add_argument(
        "--max-concurrent",
        type=_non_negative_int,
        default=None,
        help="同時に実行（起動）できるサブタスクエージェントの最大数（未指定時はTOMLまたは既定値2）",
    )


def _add_execution_cli_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--profile",
        type=str,
        default=None,
        help="この実行全体で使用するモデル・推論強度のプロファイル（タスク指定より優先）",
    )
    parser.add_argument(
        "--child-review-gate",
        choices=["required", "off"],
        default=None,
        help="子タスクのレビュー合格証跡（verdict=pass, SHA一致）を検証するゲート（既定: required）。offで検証をスキップ。",
    )
    parser.add_argument(
        "--semantic-review-mode",
        choices=["required", "advisory", "off"],
        default=None,
        help="統合PRの第2層セマンティックレビュー（既定: required）。",
    )
    parser.add_argument(
        "--force-integration-review",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="単一タスクFast-Pathでも統合PRレビューを強制する。",
    )
    parser.add_argument(
        "--allow-unsafe-agent-execution",
        action="store_true",
        default=False,
        help="ローカルCLI（claude/agy/codex）に対する承認・サンドボックスのバイパス（完全権限実行）を明示的に許可します。",
    )


def _add_cli_arguments(parser: argparse.ArgumentParser) -> None:
    _add_core_cli_arguments(parser)
    _add_execution_cli_arguments(parser)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="スケジューラ駆動ディスパッチャー: 1サイクル分の選出・dispatchを実行する"
        "（既定でラベル更新・worktree作成・エージェント起動まで行う。dry-runには--no-applyを指定）"
    )
    _add_cli_arguments(parser)
    return parser


def _get_current_git_branch(cwd: Path | None = None) -> str:
    try:
        result = run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd, check=False)
        if result.returncode == 0:
            return result.stdout.strip()
    except OSError:
        pass
    return ""


def resolve_parent_issue(cli_parent_issue: int | None, cwd: Path | None = None) -> int:
    """Resolve parent issue number: CLI flag -> branch inference -> ConfigError."""
    if cli_parent_issue is not None:
        if cli_parent_issue <= 0:
            raise config_contracts.ConfigError(
                "parent issue must be a positive integer"
            )
        return cli_parent_issue

    branch = _get_current_git_branch(cwd)
    match = re.match(r"^parent/issue-([1-9][0-9]*)$", branch)
    if match:
        return int(match.group(1))

    raise config_contracts.ConfigError(
        "the following argument is required: -p / --parent-issue "
        "(or infer from parent/issue-<int> branch)"
    )


class _PathArgs:
    def __init__(self, run_state_path: Any = None, worktree_root: Any = None):
        self.run_state_path = run_state_path
        self.worktree_root = worktree_root


def _default_resolve_dispatch_shared_paths(
    args: Any,
    cwd: Path | None,
) -> tuple[Path, Path]:
    target_cwd = cwd or Path.cwd()
    try:
        workspace = resolve_claim_workspace(
            target_cwd,
            explicit_state_path=getattr(args, "run_state_path", None),
            explicit_worktree_root=getattr(args, "worktree_root", None),
        )
        return workspace.run_state_path, workspace.worktree_root
    except (subprocess.CalledProcessError, OSError, RuntimeError):
        state_path = getattr(args, "run_state_path", None)
        state = (
            Path(state_path)
            if state_path is not None
            else target_cwd / "run_state.json"
        )
        wt_path = getattr(args, "worktree_root", None)
        wt = Path(wt_path) if wt_path is not None else target_cwd / "worktrees"
        return state.resolve(), wt.resolve()


def _resolve_paths(
    toml_data: dict[str, Any],
    primary_root: Path,
    target_cwd: Path | None = None,
    resolve_paths_fn: Callable[[Any, Path | None], tuple[Path, Path]] | None = None,
) -> dict[str, Path]:
    if resolve_paths_fn is None:
        resolve_paths_fn = _default_resolve_dispatch_shared_paths
    try:
        run_state_path, worktree_root = resolve_paths_fn(
            _PathArgs(toml_data.get("run_state_path"), toml_data.get("worktree_root")),
            target_cwd,
        )
    except (OSError, RuntimeError, subprocess.CalledProcessError) as e:
        raise config_contracts.ConfigError(
            f"unable to resolve repository workspace: {e}"
        ) from e

    paths: dict[str, Path] = {
        "run_state_path": run_state_path,
        "worktree_root": worktree_root,
    }
    defaults = {
        "log_dir": "logs",
        "events_log_path": "events.jsonl",
        "not_needed_review_state_path": "not_needed_review_state.json",
        "report_dir": ".orchestune/reports/dispatch",
    }
    for key, default_val in defaults.items():
        val = toml_data.get(key)
        paths[key] = claim_workspace.resolve_relative_to(primary_root, val, default_val)

    return paths


def _resolve_cloud_settings(
    toml_data: dict[str, Any],
) -> tuple[str | None, str | None, str | None]:
    routine_id = os.environ.get(ROUTINE_ID_ENV_VAR) or toml_data.get("routine_id")
    routine_token = os.environ.get(ROUTINE_TOKEN_ENV_VAR)
    codex_cloud_env = os.environ.get("ORCHESTUNE_CODEX_CLOUD_ENV") or toml_data.get(
        "codex_cloud_env"
    )
    return routine_id, routine_token, codex_cloud_env


def _resolve_and_build_target(
    args: Any,
    toml_data: dict[str, Any],
    paths: dict[str, Path],
    routine_id: str | None,
    routine_token: str | None,
    codex_cloud_env: str | None,
    execution_profile_config: ExecutionProfileConfig,
    build_target_fn: Callable[[TargetBuildConfig], Any] | None = None,
) -> Any:
    if build_target_fn is None:
        build_target_fn = build_dispatch_target
    dispatch_target_name = (
        args.dispatch_target
        or toml_data.get("dispatch_target")
        or resolve_default_dispatch_target_name(os.environ)
    )
    dispatch_target = build_target_fn(
        TargetBuildConfig(
            dispatch_target_name=dispatch_target_name,
            routine_id=routine_id,
            routine_token=routine_token,
            log_dir=paths["log_dir"],
            local_cmd=toml_data.get("local_cmd"),
            codex_cloud_env=codex_cloud_env,
            allow_unsafe_agent_execution=bool(args.allow_unsafe_agent_execution),
            reviewer_bot=toml_data.get("reviewer_bot", "auto"),
        )
    )
    if args.profile:
        resolved_name = extract_target_name(dispatch_target)
        validate_profile_for_target(
            args.profile, resolved_name, execution_profile_config
        )
    return dispatch_target


def _resolve_child_review_gate(args: Any, toml_data: dict[str, Any]) -> str:
    env_gate: str | None = None
    if "ORCHESTUNE_CHILD_REVIEW_GATE" in os.environ:
        raw_env = os.environ["ORCHESTUNE_CHILD_REVIEW_GATE"].strip()
        # Per design specification (#1031 section 160), invalid values across all sources
        # (including env var) are rejected as ConfigError even if shadowed by higher precedence.
        if raw_env not in VALID_CHILD_REVIEW_GATE_MODES:
            choices = ", ".join(repr(c) for c in sorted(VALID_CHILD_REVIEW_GATE_MODES))
            raise config_contracts.ConfigError(
                f"environment variable 'ORCHESTUNE_CHILD_REVIEW_GATE' must be one of: {choices}"
            )
        env_gate = raw_env

    cli_gate = getattr(args, "child_review_gate", None)
    if cli_gate is not None:
        return str(cli_gate)
    toml_gate = toml_data.get("child_review_gate")
    if toml_gate is not None:
        return str(toml_gate)
    return env_gate or "required"


def _resolve_semantic_review_mode(args: Any, toml_data: dict[str, Any]) -> str:
    env_mode: str | None = None
    if "ORCHESTUNE_SEMANTIC_REVIEW_MODE" in os.environ:
        raw_env = os.environ["ORCHESTUNE_SEMANTIC_REVIEW_MODE"].strip()
        if raw_env not in VALID_SEMANTIC_REVIEW_MODES:
            choices = ", ".join(repr(c) for c in sorted(VALID_SEMANTIC_REVIEW_MODES))
            raise config_contracts.ConfigError(
                "environment variable 'ORCHESTUNE_SEMANTIC_REVIEW_MODE' "
                f"must be one of: {choices}"
            )
        env_mode = raw_env
    cli_mode = getattr(args, "semantic_review_mode", None)
    if cli_mode is not None:
        return str(cli_mode)
    toml_mode = toml_data.get("semantic_review_mode")
    if toml_mode is not None:
        return str(toml_mode)
    if env_mode is not None:
        return env_mode
    return "off" if os.environ.get("ORCHESTUNE_SEMANTIC_REVIEW") == "0" else "required"


def _extract_semantic_config_kwargs(
    args: Any,
    toml_data: dict[str, Any],
) -> dict[str, Any]:
    semantic_keys = (
        "semantic_review_provider",
        "semantic_fix_provider",
        "semantic_review_trigger",
        "max_semantic_review_rounds",
        "max_semantic_fix_retries",
        "semantic_review_timeout_seconds",
        "semantic_fix_timeout_seconds",
        "semantic_ci_timeout_seconds",
        "semantic_attempt_timeout_seconds",
    )
    force_rev = getattr(args, "force_integration_review", None)
    return {
        "semantic_review_mode": _resolve_semantic_review_mode(args, toml_data),
        "force_integration_review": (
            force_rev
            if force_rev is not None
            else toml_data.get("force_integration_review", False)
        ),
        **{k: toml_data[k] for k in semantic_keys if k in toml_data},
    }


def _assemble_dispatcher_config(
    args: Any,
    toml_data: dict[str, Any],
    paths: dict[str, Path],
    parent_issue: int,
    dispatch_target: Any,
    execution_profile_config: ExecutionProfileConfig,
    forge: Any = None,
) -> DispatcherConfig:
    dag_ignore_patterns = compile_extra_ignore_patterns(
        extract_dag_ignore_patterns(toml_data)
    )
    extracted_similarity = extract_dag_similarity_threshold(toml_data)
    dag_similarity_threshold = (
        extracted_similarity
        if extracted_similarity is not None
        else DEFAULT_SIMILARITY_THRESHOLD
    )
    ci_raw = toml_data.get("ci_command")
    ci_cmd = parse_ci_command(ci_raw) if ci_raw is not None else None
    repair_codes = list(toml_data.get("consistency_repair_code", []))
    config_kwargs: dict[str, Any] = {
        "parent_issue_number": parent_issue,
        "apply": args.apply if args.apply is not None else toml_data.get("apply", True),
        **paths,
        "dispatch_target": dispatch_target,
        "profile": args.profile,
        "child_review_gate": _resolve_child_review_gate(args, toml_data),
        "ci_command": ci_cmd,
        "dag_ignore_patterns": dag_ignore_patterns,
        "dag_similarity_threshold": dag_similarity_threshold,
        "execution_profile_config": execution_profile_config,
        "consistency_mode": ConsistencyMode(toml_data.get("consistency_mode", "off")),
        "consistency_repair_allowlist": frozenset(repair_codes),
        **runtime_tuning_overrides(toml_data),
        **_extract_semantic_config_kwargs(args, toml_data),
    }
    if args.max_concurrent is not None:
        config_kwargs["max_concurrent"] = args.max_concurrent
    return DispatcherConfig(
        **config_kwargs, **({"forge": forge} if forge is not None else {})
    )


def load_and_resolve_config(
    cli_args: list[str] | None = None,
    cwd: Path | None = None,
    forge: Any = None,
    *,
    load_config_fn: Callable[[Path], dict[str, Any]] | None = None,
    build_target_fn: Callable[[TargetBuildConfig], Any] | None = None,
    resolve_paths_fn: Callable[[Any, Path | None], tuple[Path, Path]] | None = None,
) -> DispatcherConfig:
    """End-to-end config resolution: CLI parsing -> TOML validation -> DispatcherConfig."""
    parser = build_arg_parser()
    args = parser.parse_args(cli_args)
    target_cwd = cwd or Path.cwd()
    primary_root, checkout_root = claim_workspace.resolve_checkout_roots(target_cwd)

    if load_config_fn is None:
        load_config_fn = find_and_load_config_file
    raw_toml = load_config_fn(checkout_root)
    toml_data = validate_toml_config(raw_toml) if raw_toml else {}

    parent_issue = resolve_parent_issue(args.parent_issue, target_cwd)
    paths = _resolve_paths(
        toml_data, primary_root, target_cwd, resolve_paths_fn=resolve_paths_fn
    )
    routine_id, routine_token, codex_cloud_env = _resolve_cloud_settings(toml_data)
    exec_profile_cfg = extract_execution_profile_config(toml_data)

    dispatch_target = _resolve_and_build_target(
        args,
        toml_data,
        paths,
        routine_id,
        routine_token,
        codex_cloud_env,
        exec_profile_cfg,
        build_target_fn=build_target_fn,
    )
    config = _assemble_dispatcher_config(
        args,
        toml_data,
        paths,
        parent_issue,
        dispatch_target,
        exec_profile_cfg,
        forge=forge,
    )
    _resolve_report_override(config, target_cwd)
    return config


def _resolve_report_override(config: DispatcherConfig, target_cwd: Path) -> None:
    if "ORCHESTUNE_DISPATCH_REPORT_PATH" in os.environ:
        value = os.environ["ORCHESTUNE_DISPATCH_REPORT_PATH"]
        if not value.strip():
            raise config_contracts.ConfigError(
                "ORCHESTUNE_DISPATCH_REPORT_PATH must not be empty"
            )
        candidate = Path(value)
        config.report_path = (
            candidate if candidate.is_absolute() else target_cwd / candidate
        )
        try:
            _check_target(config.report_path, config)
        except ValueError as exc:
            raise config_contracts.ConfigError(str(exc)) from exc
