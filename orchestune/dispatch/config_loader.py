"""Configuration loading, schema validation, and precedence resolution for orchestune-dispatch."""

from __future__ import annotations

import argparse
import os
import re
import shlex
import subprocess
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any

from orchestune.claim.workspace import (
    _resolve_primary_root,
    _resolve_relative_to,
    resolve_claim_workspace,
)
from orchestune.consistency.supervisor import MAX_REPAIR_PASSES, ConsistencyMode
from orchestune.dag.models import (
    DAG_TOOL_CONFIG_KEYS,
    compile_extra_ignore_patterns,
    extract_dag_ignore_patterns,
    extract_dag_similarity_threshold,
)
from orchestune.dag.models import (
    ConfigError as ConfigError,
)
from orchestune.dag.similarity import DEFAULT_SIMILARITY_THRESHOLD
from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.execution_profiles import (
    ExecutionProfileConfig,
    extract_execution_profile_config,
    extract_target_name,
    validate_profile_for_target,
)
from orchestune.dispatch.targets import (
    ROUTINE_ID_ENV_VAR,
    ROUTINE_TOKEN_ENV_VAR,
    TargetBuildConfig,
    build_dispatch_target,
    resolve_default_dispatch_target_name,
)
from orchestune.infra.git_cli import get_git_repository_paths, run_git

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


def _add_cli_arguments(parser: argparse.ArgumentParser) -> None:
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
        "--no-applyでdry-run（何も変更しない）にできる。",
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
    parser.add_argument(
        "--profile",
        type=str,
        default=None,
        help="この実行全体で使用するモデル・推論強度のプロファイル（タスク指定より優先）",
    )
    parser.add_argument(
        "--allow-unsafe-agent-execution",
        action="store_true",
        default=False,
        help="ローカルCLI（claude/agy/codex）に対する承認・サンドボックスのバイパス（完全権限実行）を明示的に許可します。",
    )


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="スケジューラ駆動ディスパッチャー: 1サイクル分の選出・dispatchを実行する"
        "（既定でラベル更新・worktree作成・エージェント起動まで行う。dry-runには--no-applyを指定）"
    )
    _add_cli_arguments(parser)
    return parser


_PROHIBITED_TOML_KEYS: dict[str, str] = {
    "routine_token": (
        "setting 'routine_token' is prohibited in configuration files; "
        f"set the {ROUTINE_TOKEN_ENV_VAR} environment variable instead"
    ),
    "routine-token": (
        "setting 'routine-token' is prohibited in configuration files; "
        f"set the {ROUTINE_TOKEN_ENV_VAR} environment variable instead"
    ),
    "allow_unsafe_agent_execution": (
        "setting 'allow_unsafe_agent_execution' is prohibited in configuration files; "
        "use the --allow-unsafe-agent-execution CLI flag"
    ),
    "allow-unsafe-agent-execution": (
        "setting 'allow-unsafe-agent-execution' is prohibited in configuration files; "
        "use the --allow-unsafe-agent-execution CLI flag"
    ),
    "parent_issue": (
        "parent issue cannot be set in configuration files; "
        "pass -p / --parent-issue on the CLI or infer from a parent/issue-<N> branch"
    ),
    "parent-issue": (
        "parent issue cannot be set in configuration files; "
        "pass -p / --parent-issue on the CLI or infer from a parent/issue-<N> branch"
    ),
    "parent_issue_number": (
        "parent issue cannot be set in configuration files; "
        "pass -p / --parent-issue on the CLI or infer from a parent/issue-<N> branch"
    ),
    "parent-issue-number": (
        "parent issue cannot be set in configuration files; "
        "pass -p / --parent-issue on the CLI or infer from a parent/issue-<N> branch"
    ),
    "model": (
        "top-level 'model' is deprecated and prohibited in configuration files; "
        "define models inside [execution_profiles.<name>.<target>]"
    ),
    "reasoning_effort": (
        "top-level 'reasoning_effort' is deprecated and prohibited in configuration files; "
        "define reasoning_effort inside [execution_profiles.<name>.<target>]"
    ),
    "reasoning-effort": (
        "top-level 'reasoning-effort' is deprecated and prohibited in configuration files; "
        "define reasoning_effort inside [execution_profiles.<name>.<target>]"
    ),
    "effort": (
        "top-level 'effort' is deprecated and prohibited in configuration files; "
        "define reasoning_effort inside [execution_profiles.<name>.<target>]"
    ),
}

_PATH_CONFIG_KEYS = frozenset(
    {
        "run_state_path",
        "worktree_root",
        "log_dir",
        "events_log_path",
        "not_needed_review_state_path",
    }
)

_NON_NEGATIVE_INT_KEYS = frozenset(
    {
        "max_concurrent",
        "max_launches_per_window",
        "deviation_buffer_lines",
        "max_recompute_retries",
        "task_timeout_seconds",
        "max_task_reclaims",
        "early_death_window_seconds",
        "max_early_death_retries",
        "early_death_backoff_seconds",
        "not_needed_review_timeout_seconds",
        "max_tokens_per_window",
        "max_tokens_per_task",
    }
)

_POSITIVE_INT_KEYS = frozenset({"window_seconds"})

_BOOLEAN_CONFIG_KEYS = frozenset({"apply", "zombie_gc"})

_STRING_KEYS = frozenset(
    {"local_cmd", "routine_id", "codex_cloud_env", "default_execution_profile"}
)

_TARGET_CHOICES = frozenset(
    {
        "local",
        "cloud-routine",
        "codex-cloud",
        "claude-cli",
        "agy-cli",
        "codex-cli",
        "auto",
    }
)

_REVIEWER_BOT_CHOICES = frozenset({"auto", "claude", "codex"})

_CONSISTENCY_MODE_CHOICES = frozenset({"off", "shadow", "repair"})

_EXECUTION_PROFILE_TABLE_KEYS = frozenset(
    {"execution_profiles", "execution-profiles", "model_tiers", "model-tiers"}
)


def _normalize_key(key: str) -> str:
    return key.replace("-", "_")


def _validate_scalar_entry(normalized_key: str, raw_key: str, value: Any) -> Any:
    if normalized_key in _BOOLEAN_CONFIG_KEYS:
        if not isinstance(value, bool):
            raise ConfigError(f"{raw_key!r} must be a boolean")
        return value
    if normalized_key in _PATH_CONFIG_KEYS:
        if not isinstance(value, str | Path):
            raise ConfigError(f"{raw_key!r} must be a string path")
        return Path(value)
    if not isinstance(value, str):
        raise ConfigError(f"{raw_key!r} must be a string")
    return value


def _validate_numeric_entry(normalized_key: str, raw_key: str, value: Any) -> Any:
    if normalized_key == "consistency_max_repair_passes":
        if not isinstance(value, int) or isinstance(value, bool):
            raise ConfigError(f"{raw_key!r} must be an integer")
        if not 1 <= value <= MAX_REPAIR_PASSES:
            raise ConfigError(f"{raw_key!r} must be between 1 and {MAX_REPAIR_PASSES}")
        return value
    if normalized_key in _NON_NEGATIVE_INT_KEYS:
        if value is None:
            return None
        if not isinstance(value, int) or isinstance(value, bool):
            raise ConfigError(f"{raw_key!r} must be an integer")
        if value < 0:
            raise ConfigError(f"{raw_key!r} must be greater than or equal to 0")
        return value
    if not isinstance(value, int) or isinstance(value, bool):
        raise ConfigError(f"{raw_key!r} must be an integer")
    if value < 1:
        raise ConfigError(f"{raw_key!r} must be greater than or equal to 1")
    return value


def _validate_choice_entry(normalized_key: str, raw_key: str, value: Any) -> Any:
    choice_map = {
        "dispatch_target": _TARGET_CHOICES,
        "reviewer_bot": _REVIEWER_BOT_CHOICES,
        "consistency_mode": _CONSISTENCY_MODE_CHOICES,
    }
    allowed = choice_map[normalized_key]
    if not isinstance(value, str) or value not in allowed:
        choices = ", ".join(repr(c) for c in sorted(allowed))
        raise ConfigError(f"{raw_key!r} must be one of: {choices}")
    return value


def _validate_compound_entry(normalized_key: str, raw_key: str, value: Any) -> Any:
    if normalized_key == "consistency_repair_code":
        if not isinstance(value, list) or not all(
            isinstance(item, str) and item for item in value
        ):
            raise ConfigError(f"{raw_key!r} must be a list of non-empty strings")
        return value
    if normalized_key == "ci_command":
        if not isinstance(value, str | list):
            raise ConfigError(f"{raw_key!r} must be a string or list of strings")
        return value
    if raw_key in DAG_TOOL_CONFIG_KEYS:
        return value
    if (
        raw_key in _EXECUTION_PROFILE_TABLE_KEYS
        or normalized_key in _EXECUTION_PROFILE_TABLE_KEYS
    ):
        if not isinstance(value, dict):
            raise ConfigError(f"{raw_key!r} must be a table")
        return value
    raise ConfigError(f"unknown key {raw_key!r}")


def _validate_single_entry(normalized_key: str, raw_key: str, value: Any) -> Any:
    if (
        normalized_key in _BOOLEAN_CONFIG_KEYS
        or normalized_key in _PATH_CONFIG_KEYS
        or normalized_key in _STRING_KEYS
    ):
        return _validate_scalar_entry(normalized_key, raw_key, value)
    if (
        normalized_key in _NON_NEGATIVE_INT_KEYS
        or normalized_key in _POSITIVE_INT_KEYS
        or normalized_key == "consistency_max_repair_passes"
    ):
        return _validate_numeric_entry(normalized_key, raw_key, value)
    if normalized_key in {"dispatch_target", "reviewer_bot", "consistency_mode"}:
        return _validate_choice_entry(normalized_key, raw_key, value)
    return _validate_compound_entry(normalized_key, raw_key, value)


def validate_toml_config(config_data: dict[str, Any]) -> dict[str, Any]:
    """Validate TOML dictionary against the Dispatcher configuration schema."""
    validated: dict[str, Any] = {}
    for raw_key, value in config_data.items():
        if raw_key in _PROHIBITED_TOML_KEYS:
            raise ConfigError(_PROHIBITED_TOML_KEYS[raw_key])
        normalized = _normalize_key(raw_key)
        if normalized in _PROHIBITED_TOML_KEYS:
            raise ConfigError(_PROHIBITED_TOML_KEYS[normalized])

        val = _validate_single_entry(normalized, raw_key, value)
        validated[normalized] = val
        if raw_key != normalized:
            validated[raw_key] = val

    if (
        "dag_similarity_threshold" in config_data
        or "dag-similarity-threshold" in config_data
    ):
        extract_dag_similarity_threshold(config_data)
    if "dag_ignore_patterns" in config_data or "dag-ignore-patterns" in config_data:
        patterns = extract_dag_ignore_patterns(config_data)
        compile_extra_ignore_patterns(patterns)
    if (
        "execution_profiles" in config_data
        or "execution-profiles" in config_data
        or "model_tiers" in config_data
        or "model-tiers" in config_data
    ):
        extract_execution_profile_config(config_data)

    return validated


def find_and_load_config_file(checkout_root: Path) -> dict[str, Any]:
    """Search and load configuration from orchestune.toml or pyproject.toml."""
    orchestune_toml = checkout_root / "orchestune.toml"
    if orchestune_toml.exists():
        try:
            with open(orchestune_toml, "rb") as f:
                return tomllib.load(f)
        except Exception as e:
            raise ConfigError(f"failed to load {orchestune_toml}: {e}") from e

    pyproject_toml = checkout_root / "pyproject.toml"
    if pyproject_toml.exists():
        try:
            with open(pyproject_toml, "rb") as f:
                data = tomllib.load(f)
        except Exception as e:
            raise ConfigError(f"failed to load {pyproject_toml}: {e}") from e
        tool = data.get("tool", {})
        if not isinstance(tool, dict):
            raise ConfigError(f"{pyproject_toml}: [tool] must be a table")
        config = tool.get("orchestune", {})
        if not isinstance(config, dict):
            raise ConfigError(f"{pyproject_toml}: [tool.orchestune] must be a table")
        return config

    return {}


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
            raise ConfigError("parent issue must be a positive integer")
        return cli_parent_issue

    branch = _get_current_git_branch(cwd)
    match = re.match(r"^parent/issue-([1-9][0-9]*)$", branch)
    if match:
        return int(match.group(1))

    raise ConfigError(
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
        raise ConfigError(f"unable to resolve repository workspace: {e}") from e

    paths: dict[str, Path] = {
        "run_state_path": run_state_path,
        "worktree_root": worktree_root,
    }
    defaults = {
        "log_dir": "logs",
        "events_log_path": "events.jsonl",
        "not_needed_review_state_path": "not_needed_review_state.json",
    }
    for key, default_val in defaults.items():
        val = toml_data.get(key)
        paths[key] = _resolve_relative_to(primary_root, val, default_val)

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


def _resolve_checkout_roots(target_cwd: Path) -> tuple[Path, Path]:
    try:
        toplevel, common_dir = get_git_repository_paths(target_cwd)
        primary_root = _resolve_primary_root(toplevel, common_dir)
        checkout_root = toplevel
    except (subprocess.CalledProcessError, OSError, RuntimeError):
        primary_root = target_cwd.resolve()
        checkout_root = target_cwd.resolve()
    return primary_root, checkout_root


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


def _build_runtime_tuning_kwargs(toml_data: dict[str, Any]) -> dict[str, Any]:
    return {
        "max_launches_per_window": toml_data.get("max_launches_per_window", 1),
        "window_seconds": toml_data.get("window_seconds", 3600),
        "deviation_buffer_lines": toml_data.get("deviation_buffer_lines", 5),
        "max_recompute_retries": toml_data.get("max_recompute_retries", 2),
        "task_timeout_seconds": toml_data.get("task_timeout_seconds", 0),
        "max_task_reclaims": toml_data.get("max_task_reclaims", 3),
        "early_death_window_seconds": toml_data.get("early_death_window_seconds", 120),
        "max_early_death_retries": toml_data.get("max_early_death_retries", 2),
        "early_death_backoff_seconds": toml_data.get("early_death_backoff_seconds", 60),
        "zombie_gc": toml_data.get("zombie_gc", True),
        "max_tokens_per_window": toml_data.get("max_tokens_per_window"),
        "max_tokens_per_task": toml_data.get("max_tokens_per_task"),
        "not_needed_review_timeout_seconds": toml_data.get(
            "not_needed_review_timeout_seconds", 86400
        ),
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
    ci_cmd = shlex.split(ci_raw) if isinstance(ci_raw, str) else ci_raw
    repair_codes = list(toml_data.get("consistency_repair_code", []))

    config_kwargs: dict[str, Any] = {
        "parent_issue_number": parent_issue,
        "apply": args.apply if args.apply is not None else toml_data.get("apply", True),
        "max_concurrent": (
            args.max_concurrent
            if args.max_concurrent is not None
            else toml_data.get("max_concurrent", 2)
        ),
        "run_state_path": paths["run_state_path"],
        "worktree_root": paths["worktree_root"],
        "log_dir": paths["log_dir"],
        "events_log_path": paths["events_log_path"],
        "not_needed_review_state_path": paths["not_needed_review_state_path"],
        "dispatch_target": dispatch_target,
        "profile": args.profile,
        "ci_command": ci_cmd,
        "dag_ignore_patterns": dag_ignore_patterns,
        "dag_similarity_threshold": dag_similarity_threshold,
        "execution_profile_config": execution_profile_config,
        "consistency_mode": ConsistencyMode(toml_data.get("consistency_mode", "off")),
        "consistency_repair_allowlist": frozenset(repair_codes),
        "consistency_max_repair_passes": toml_data.get(
            "consistency_max_repair_passes", 1
        ),
        **_build_runtime_tuning_kwargs(toml_data),
    }
    if forge is not None:
        config_kwargs["forge"] = forge
    return DispatcherConfig(**config_kwargs)


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
    primary_root, checkout_root = _resolve_checkout_roots(target_cwd)

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
    return _assemble_dispatcher_config(
        args,
        toml_data,
        paths,
        parent_issue,
        dispatch_target,
        exec_profile_cfg,
        forge=forge,
    )
