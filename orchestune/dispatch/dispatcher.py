from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

from orchestune.dag.models import (
    DAG_TOOL_CONFIG_KEYS,
    compile_extra_ignore_patterns,
    extract_dag_ignore_patterns,
    extract_dag_similarity_threshold,
    load_orchestune_config,
)
from orchestune.dag.similarity import DEFAULT_SIMILARITY_THRESHOLD
from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.cycle import run_dispatch_cycle
from orchestune.dispatch.postcycle import (
    _decide_semantic_review_enabled,
    _poll_pending_not_needed_reviews,
    _post_event_log_comment,
    _process_parent_completion,
    _run_semantic_integrator,
)
from orchestune.dispatch.report import _report_to_dict, write_github_step_summary
from orchestune.dispatch.result import PhaseResult, PhaseStatus
from orchestune.dispatch.targets import (
    TargetBuildConfig,
    build_dispatch_target,
    resolve_default_dispatch_target_name,
)
from orchestune.forge import ForgeAuthError


def _non_negative_int(value: str) -> int:
    """#512/PR#520レビュー対応(Codex P2): 0以上の整数のみを受理するargparse型。

    `type=int`のままだと`--max-task-reclaims -1`のような負値がそのまま通り、
    「1回目の回収で必ず上限超過」と解釈されてタスクが黙って
    `status:blocked-human-review`へ落ちてしまう（設定ファイル側は
    `_config_defaults`が同じ制約を検証しており、CLIだけが素通りしていた）。
    """
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError(
            f"must be greater than or equal to 0 (got {parsed})"
        )
    return parsed


def _add_execution_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--apply",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="実際にラベル更新・worktree作成・エージェント起動を行う（既定）。"
        "--no-applyでdry-run（何も変更しない）にできる。",
    )
    parser.add_argument("--max-concurrent", type=int, default=2)
    parser.add_argument("--max-launches-per-window", type=int, default=1)
    parser.add_argument("--window-seconds", type=int, default=3600)
    parser.add_argument("--parent-issue", type=int, default=None)
    parser.add_argument(
        "--deviation-buffer-lines",
        type=int,
        default=5,
        help="footprint逸脱として扱わない変更行数の許容バッファ（#200: ライブロック防止）",
    )
    parser.add_argument(
        "--max-recompute-retries",
        type=int,
        default=2,
        help="DAG再計算のリトライ上限。超過時は強制直列化にフォールバックする（#200）",
    )
    parser.add_argument(
        "--task-timeout-seconds",
        type=int,
        default=0,
        help="ゾンビ・タイムアウトGCを実行するタスクのタイムアウト秒数（0でタイムアウトGCは無効、ゾンビ検知のみ実行）",
    )
    parser.add_argument(
        "--max-task-reclaims",
        type=_non_negative_int,
        default=3,
        help="ゾンビ・タイムアウトGCが同一タスクをstatus:queuedへ差し戻せる回数の上限"
        "（#512）。超過したタスクはstatus:blocked-human-reviewへ遷移し再投入されなくなる。"
        "0を指定すると1回目の回収で即エスカレーションする（無制限にはできない）",
    )
    parser.add_argument(
        "--zombie-gc",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="ゾンビプロセスの検知・回収を行うかどうか（デフォルト: True）",
    )


def _add_storage_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--run-state-path", type=Path, default=Path("run_state.json"))
    parser.add_argument("--worktree-root", type=Path, default=Path("worktrees"))
    parser.add_argument("--log-dir", type=Path, default=Path("logs"))
    parser.add_argument(
        "--events-log-path",
        type=Path,
        default=Path("events.jsonl"),
        help="#239: KPI集計用の構造化イベントログ（JSON Lines）の出力先",
    )
    parser.add_argument(
        "--not-needed-review-state-path",
        type=Path,
        default=Path("not_needed_review_state.json"),
        help="#282: 保留中のstatus:not-needed検証レビュー（合否ポーリング・自動クローズ待ち）の永続化先",
    )
    parser.add_argument(
        "--not-needed-review-timeout-seconds",
        type=_non_negative_int,
        default=86400,
        help="#511: status:not-needed検証レビューがどちらの結果ラベルも返さないまま"
        "保持され続ける秒数の上限。超過したエントリはstatus:blocked-human-reviewへ"
        "エスカレーションする（無制限にはできない）",
    )


_DISPATCH_TARGET_HELP = (
    "#215/#163: エージェントの実ディスパッチ先。未指定時は実行環境から自動選択される"
    "（GitHub Actions実行時は'cloud-routine'、ローカル実行時は'auto'）。"
    "'auto'はPATH上のローカルCLIを検出し（claude優先、次点agy、codex）、"
    "見つかったCLIへディスパッチする。未検出時は警告を出しダミー起動にフォールバック。"
    "'local'はダミー起動（no-op）。'cloud-routine'はClaude Codeクラウドルーチンへディスパッチ。"
    "'codex-cloud'はCodex Cloud CLIへディスパッチ。"
    "'claude-cli'/'agy-cli'/'codex-cli'はローカルCLIへディスパッチする。"
)


def _add_dispatch_target_arguments(parser: argparse.ArgumentParser) -> None:
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
        "--local-cmd",
        default=None,
        help="ローカルCLIにディスパッチする際のコマンドテンプレート。"
        "使用可能な変数: {issue_number}, {subtask_id}, {branch_name}, {worktree_path}。",
    )
    parser.add_argument(
        "--routine-id",
        default=None,
        help="#215: クラウドルーチンのID（未指定時はORCHESTUNE_ROUTINE_ID環境変数を使用）",
    )
    parser.add_argument(
        "--routine-token",
        default=None,
        help="#215: クラウドルーチンのAPIトークン（未指定時はORCHESTUNE_ROUTINE_TOKEN環境変数を使用）",
    )
    parser.add_argument(
        "--codex-cloud-env",
        default=None,
        help="Codex Cloudのenvironment ID（未指定時はORCHESTUNE_CODEX_CLOUD_ENV環境変数を使用）",
    )


def _add_safety_and_budget_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--allow-unsafe-agent-execution",
        action="store_true",
        help="ローカルCLI（claude/agy/codex）に対する承認・サンドボックスのバイパス（完全権限実行）を明示的に許可します。",
    )
    parser.add_argument(
        "--max-tokens-per-window",
        type=int,
        default=None,
        help="#438: ウィンドウ内の総トークン消費上限（超過時は新規タスク起動を停止）",
    )
    parser.add_argument(
        "--max-tokens-per-task",
        type=int,
        default=None,
        help="#438: 単一サブタスクのトークン消費上限（超過時はstatus:blocked-human-reviewへエスカレーション）",
    )
    parser.add_argument(
        "--ci-command",
        default=None,
        help="#394: Integratorが統合ブランチ上で実行するCIコマンド（shlex構文の"
        "シェル風文字列。例: './scripts/local-ci.sh' や 'make ci'）。"
        "未指定時はOrchestune自身のリポジトリ固有の既定値"
        "（./scripts/local-ci.sh）にフォールバックする。",
    )


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="スケジューラ駆動ディスパッチャー: 1サイクル分の選出・dispatchを実行する"
        "（既定でラベル更新・worktree作成・エージェント起動まで行う。dry-runには--no-applyを指定）"
    )
    _add_execution_arguments(parser)
    _add_storage_arguments(parser)
    _add_dispatch_target_arguments(parser)
    _add_safety_and_budget_arguments(parser)
    return parser


def load_config_file(cwd: Path | None = None) -> dict[str, Any]:
    """Load the first dispatcher configuration file found in *cwd*.

    Configuration syntax errors are deliberately fatal to the caller: falling
    through to another file or to CLI defaults would make a misspelled setting
    look like a successful dispatch.

    Delegates the actual orchestune.toml/pyproject.toml discovery to
    `dag_models.load_orchestune_config`, shared with orchestune-dag
    (dag_cli.py) and orchestune-provision (provisioning.py) so all three
    agree on the same discovery order and error semantics (#404 review).
    """
    if cwd is None:
        cwd = Path.cwd()
    return load_orchestune_config(cwd)


def _config_error(parser: argparse.ArgumentParser, message: str) -> NoReturn:
    parser.error(f"invalid dispatcher config: {message}")


# orchestune.toml / pyproject.toml の [tool.orchestune] は本来dispatcher専用の
# 設定名前空間だが、orchestune-dag CLI（dag_cli.py）・orchestune-provision
# （provisioning.py）も同じファイル・セクションから dag_ignore_patterns /
# dag_similarity_threshold を読み込む（#398/#407）。dispatcherの未知キー検知に
# 巻き込まれて `orchestune-dispatch` がクラッシュしないよう、他ツール由来と
# 判明しているキーはここで無視する（dispatcher自身の設定としては使用しない値
# としてargparseの未知キー検知をスキップするだけで、DispatcherConfigへの
# 実際の反映はmain()が個別に行う）。
#
# #415レビュー指摘: `dag_`prefixによる無条件許可は、`dag_ignore_pattern`
# （末尾のs脱落）のようなtypoまで黙って見逃してしまい、設定が効いていない
# ことにユーザーが気づけなくなる。既知の共有DAGキー名（`dag_models.py`の
# `DAG_TOOL_CONFIG_KEYS`、extract_*関数のすぐ側で一元管理）との完全一致で
# のみ無視し、それ以外の`dag_`始まりキーは引き続き"unknown key"として
# 拒否する。
_NON_DISPATCHER_CONFIG_KEYS = DAG_TOOL_CONFIG_KEYS


def _normalize_config_key(key: str) -> str:
    normalized_key = key.replace("-", "_")
    if normalized_key == "parent_issue_number":
        return "parent_issue"
    return normalized_key


def _is_non_dispatcher_config_key(raw_key: str) -> bool:
    # #415レビュー再指摘: 正規化後（ハイフン→アンダースコア変換後）の
    # キーではなく、config_dataの生のキー文字列と比較する。正規化後の
    # キーで比較すると、`dag_similarity-threshold`のような区切り文字
    # 混在のtypoまで正規のスペリングへ丸め込まれて"unknown key"検知を
    # すり抜けてしまう（extract_*関数は生のキーでしか値を読まないため、
    # その値は結局どこにも読み取られずサイレントに無視される）。
    return raw_key in _NON_DISPATCHER_CONFIG_KEYS


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
        "not_needed_review_timeout_seconds",
    }
)
_POSITIVE_INT_KEYS = frozenset({"window_seconds", "parent_issue"})
_BOOLEAN_CONFIG_KEYS = frozenset({"apply", "zombie_gc", "allow_unsafe_agent_execution"})


def _validate_config_entry(
    parser: argparse.ArgumentParser,
    action: argparse.Action,
    normalized_key: str,
    key: str,
    value: Any,
) -> Any:
    if normalized_key in _BOOLEAN_CONFIG_KEYS:
        if not isinstance(value, bool):
            _config_error(parser, f"{key!r} must be a boolean")
        return value
    if normalized_key in _PATH_CONFIG_KEYS:
        if not isinstance(value, str):
            _config_error(parser, f"{key!r} must be a string path")
        return Path(value)
    if normalized_key in _NON_NEGATIVE_INT_KEYS | _POSITIVE_INT_KEYS:
        if not isinstance(value, int) or isinstance(value, bool):
            _config_error(parser, f"{key!r} must be an integer")
        if normalized_key in _NON_NEGATIVE_INT_KEYS and value < 0:
            _config_error(parser, f"{key!r} must be greater than or equal to 0")
        if normalized_key in _POSITIVE_INT_KEYS and value < 1:
            _config_error(parser, f"{key!r} must be greater than or equal to 1")
        return value
    if action.choices is not None:
        if not isinstance(value, str) or value not in action.choices:
            choices = ", ".join(repr(choice) for choice in action.choices)
            _config_error(parser, f"{key!r} must be one of: {choices}")
        return value
    if not isinstance(value, str):
        _config_error(parser, f"{key!r} must be a string")
    return value


def _config_defaults(
    parser: argparse.ArgumentParser, config_data: dict[str, Any]
) -> dict[str, Any]:
    """Validate TOML values before using them as argparse defaults."""
    actions = {action.dest: action for action in parser._actions}
    defaults: dict[str, Any] = {}

    for key, value in config_data.items():
        if _is_non_dispatcher_config_key(key):
            continue
        normalized_key = _normalize_config_key(key)
        action = actions.get(normalized_key)
        if action is None or normalized_key == "help":
            _config_error(parser, f"unknown key {key!r}")

        defaults[normalized_key] = _validate_config_entry(
            parser, action, normalized_key, key, value
        )

    return defaults


@dataclass(frozen=True)
class _DispatcherInputs:
    args: argparse.Namespace
    dag_ignore_patterns: tuple[re.Pattern[str], ...]
    dag_similarity_threshold: float


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
        config_data = load_config_file(cwd)
    except ValueError as e:
        _config_error(parser, str(e))
    if config_data:
        parser.set_defaults(**_config_defaults(parser, config_data))

    try:
        dag_ignore_patterns = compile_extra_ignore_patterns(
            extract_dag_ignore_patterns(config_data)
        )
        config_dag_similarity_threshold = extract_dag_similarity_threshold(config_data)
    except (ValueError, re.error) as e:
        _config_error(parser, str(e))
    dag_similarity_threshold = (
        config_dag_similarity_threshold
        if config_dag_similarity_threshold is not None
        else DEFAULT_SIMILARITY_THRESHOLD
    )
    return _DispatcherInputs(
        args=parser.parse_args(argv),
        dag_ignore_patterns=dag_ignore_patterns,
        dag_similarity_threshold=dag_similarity_threshold,
    )


def _build_dispatcher_config(inputs: _DispatcherInputs) -> DispatcherConfig:
    args = inputs.args
    dispatch_target_name = args.dispatch_target or resolve_default_dispatch_target_name(
        os.environ
    )
    return DispatcherConfig(
        max_concurrent=args.max_concurrent,
        max_launches_per_window=args.max_launches_per_window,
        window_seconds=args.window_seconds,
        run_state_path=args.run_state_path,
        worktree_root=args.worktree_root,
        log_dir=args.log_dir,
        events_log_path=args.events_log_path,
        parent_issue_number=args.parent_issue,
        apply=args.apply,
        dispatch_target=build_dispatch_target(
            TargetBuildConfig(
                dispatch_target_name,
                args.routine_id,
                args.routine_token,
                args.log_dir,
                local_cmd=args.local_cmd,
                codex_cloud_env=args.codex_cloud_env,
                allow_unsafe_agent_execution=args.allow_unsafe_agent_execution,
            )
        ),
        deviation_buffer_lines=args.deviation_buffer_lines,
        max_recompute_retries=args.max_recompute_retries,
        task_timeout_seconds=args.task_timeout_seconds,
        max_task_reclaims=args.max_task_reclaims,
        zombie_gc=args.zombie_gc,
        max_tokens_per_window=args.max_tokens_per_window,
        max_tokens_per_task=args.max_tokens_per_task,
        not_needed_review_state_path=args.not_needed_review_state_path,
        not_needed_review_timeout_seconds=args.not_needed_review_timeout_seconds,
        ci_command=shlex.split(args.ci_command) if args.ci_command else None,
        dag_ignore_patterns=inputs.dag_ignore_patterns,
        dag_similarity_threshold=inputs.dag_similarity_threshold,
    )


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
        if config.parent_issue_number is not None:
            post_cycle_results.append(
                _process_parent_completion(config, auth_error=auth_error)
            )
            post_cycle_results.append(
                _post_event_log_comment(config, report, auth_error=auth_error)
            )

    return _DispatcherRunResult(
        report=report,
        post_cycle_results=post_cycle_results,
        integrator_run_report=integrator_run_report,
    )


def _emit_dispatcher_report(result: _DispatcherRunResult) -> None:
    final_dict = _report_to_dict(result.report)
    final_dict["post_cycle_results"] = [
        phase.to_dict() for phase in result.post_cycle_results
    ]
    print(json.dumps(final_dict, ensure_ascii=False, indent=2))

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
    inputs = _load_dispatcher_inputs(parser, argv, cwd)

    try:
        config = _build_dispatcher_config(inputs)
    except ValueError as e:
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
