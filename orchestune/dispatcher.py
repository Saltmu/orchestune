from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sys
from pathlib import Path
from typing import Any, NoReturn

from orchestune.dag_models import (
    DAG_TOOL_CONFIG_KEYS,
    compile_extra_ignore_patterns,
    extract_dag_ignore_patterns,
    extract_dag_similarity_threshold,
    load_orchestune_config,
)
from orchestune.dag_similarity import DEFAULT_SIMILARITY_THRESHOLD
from orchestune.dispatch_config import DispatcherConfig
from orchestune.dispatch_cycle import run_dispatch_cycle
from orchestune.dispatch_postcycle import (
    _decide_semantic_review_enabled,
    _poll_pending_not_needed_reviews,
    _post_event_log_comment,
    _process_parent_completion,
    _run_semantic_integrator,
)
from orchestune.dispatch_report import _report_to_dict, write_github_step_summary
from orchestune.dispatch_result import PhaseResult, PhaseStatus
from orchestune.dispatch_targets import (
    build_dispatch_target,
    resolve_default_dispatch_target_name,
)
from orchestune.forge import ForgeAuthError, GitHubForge


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="スケジューラ駆動ディスパッチャー: 1サイクル分の選出・dispatchを実行する"
        "（既定でラベル更新・worktree作成・エージェント起動まで行う。dry-runには--no-applyを指定）"
    )
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
    parser.add_argument("--run-state-path", type=Path, default=Path("run_state.json"))
    parser.add_argument("--worktree-root", type=Path, default=Path("worktrees"))
    parser.add_argument("--log-dir", type=Path, default=Path("logs"))
    parser.add_argument(
        "--events-log-path",
        type=Path,
        default=Path("events.jsonl"),
        help="#239: KPI集計用の構造化イベントログ（JSON Lines）の出力先",
    )
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
        "--zombie-gc",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="ゾンビプロセスの検知・回収を行うかどうか（デフォルト: True）",
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
        help="#215/#163: エージェントの実ディスパッチ先。未指定時は実行環境から自動選択される"
        "（GitHub Actions実行時（GITHUB_ACTIONS=true）は'cloud-routine'、"
        "それ以外のローカル/対話実行時は'auto'）。"
        "'auto'はPATH上にインストールされているローカルCLIを検出し"
        "（'claude'優先、次点'agy'、'codex'）、見つかったCLIへ'claude-cli'/"
        "'agy-cli'/'codex-cli'を指定した場合と同様にディスパッチする。"
        "いずれも見つからない場合は警告を出し、後方互換のダミー起動（no-op）に"
        "フォールバックする。"
        "明示的に'local'を指定した場合のみ、後方互換のダミー起動（no-op、"
        "テスト・dry-run用途）になる。'cloud-routine'はClaude Codeクラウド"
        "ルーチンのfire APIへディスパッチする（要 --routine-id/--routine-token または"
        "ORCHESTUNE_ROUTINE_ID/ORCHESTUNE_ROUTINE_TOKEN環境変数）。"
        "'codex-cloud'はCodex Cloud CLIへディスパッチする（要 --codex-cloud-env または"
        "ORCHESTUNE_CODEX_CLOUD_ENV環境変数）。"
        "'claude-cli'/'agy-cli'/'codex-cli'はそれぞれローカルのclaude/agy/codex "
        "CLIへ、許可プロンプトを毎回バイパスするプリセットのコマンドテンプレートで"
        "ディスパッチする（--local-cmdで上書き可能）",
    )
    parser.add_argument(
        "--local-cmd",
        default=None,
        help="ローカルのCLI（agyなど）にディスパッチする際のコマンドテンプレート。"
        "例: 'agy --issue {issue_number}' や 'agy'。"
        "使用可能な変数: {issue_number}, {subtask_id}, {branch_name}, {worktree_path}。"
        "--dispatch-target claude-cli/agy-cli使用時は未指定ならプリセットが使われる。",
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
    parser.add_argument(
        "--not-needed-review-state-path",
        type=Path,
        default=Path("not_needed_review_state.json"),
        help="#282: 保留中のstatus:not-needed検証レビュー（合否ポーリング・自動クローズ待ち）の永続化先",
    )
    parser.add_argument(
        "--allow-unsafe-agent-execution",
        action="store_true",
        help="ローカルCLI（claude/agy/codex）に対する承認・サンドボックスのバイパス（完全権限実行）を明示的に許可します。",
    )
    parser.add_argument(
        "--ci-command",
        default=None,
        help="#394: Integratorが統合ブランチ上で実行するCIコマンド（shlex構文の"
        "シェル風文字列。例: './scripts/local-ci.sh' や 'make ci'）。"
        "未指定時はOrchestune自身のリポジトリ固有の既定値"
        "（./scripts/local-ci.sh）にフォールバックする。",
    )
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


def _config_defaults(
    parser: argparse.ArgumentParser, config_data: dict[str, Any]
) -> dict[str, Any]:
    """Validate TOML values before using them as argparse defaults."""
    actions = {action.dest: action for action in parser._actions}
    path_keys = {
        "run_state_path",
        "worktree_root",
        "log_dir",
        "events_log_path",
        "not_needed_review_state_path",
    }
    non_negative_int_keys = {
        "max_concurrent",
        "max_launches_per_window",
        "deviation_buffer_lines",
        "max_recompute_retries",
        "task_timeout_seconds",
    }
    positive_int_keys = {"window_seconds", "parent_issue"}
    defaults: dict[str, Any] = {}

    for key, value in config_data.items():
        if _is_non_dispatcher_config_key(key):
            continue
        normalized_key = _normalize_config_key(key)
        action = actions.get(normalized_key)
        if action is None or normalized_key == "help":
            _config_error(parser, f"unknown key {key!r}")

        if normalized_key in {"apply", "zombie_gc", "allow_unsafe_agent_execution"}:
            if not isinstance(value, bool):
                _config_error(parser, f"{key!r} must be a boolean")
        elif normalized_key in path_keys:
            if not isinstance(value, str):
                _config_error(parser, f"{key!r} must be a string path")
            value = Path(value)
        elif normalized_key in non_negative_int_keys | positive_int_keys:
            if not isinstance(value, int) or isinstance(value, bool):
                _config_error(parser, f"{key!r} must be an integer")
            if normalized_key in non_negative_int_keys and value < 0:
                _config_error(parser, f"{key!r} must be greater than or equal to 0")
            if normalized_key in positive_int_keys and value < 1:
                _config_error(parser, f"{key!r} must be greater than or equal to 1")
        elif action.choices is not None:
            if not isinstance(value, str) or value not in action.choices:
                choices = ", ".join(repr(choice) for choice in action.choices)
                _config_error(parser, f"{key!r} must be one of: {choices}")
        elif not isinstance(value, str):
            _config_error(parser, f"{key!r} must be a string")

        defaults[normalized_key] = value

    return defaults


def main(argv: list[str] | None = None, cwd: Path | None = None) -> int:
    parser = _build_arg_parser()

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

    args = parser.parse_args(argv)
    dispatch_target_name = args.dispatch_target or resolve_default_dispatch_target_name(
        os.environ
    )

    try:
        config = DispatcherConfig(
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
                dispatch_target_name,
                args.routine_id,
                args.routine_token,
                args.log_dir,
                local_cmd=args.local_cmd,
                codex_cloud_env=args.codex_cloud_env,
                allow_unsafe_agent_execution=args.allow_unsafe_agent_execution,
            ),
            deviation_buffer_lines=args.deviation_buffer_lines,
            max_recompute_retries=args.max_recompute_retries,
            task_timeout_seconds=args.task_timeout_seconds,
            zombie_gc=args.zombie_gc,
            not_needed_review_state_path=args.not_needed_review_state_path,
            ci_command=shlex.split(args.ci_command) if args.ci_command else None,
            dag_ignore_patterns=dag_ignore_patterns,
            dag_similarity_threshold=dag_similarity_threshold,
        )
    except ValueError as e:
        _config_error(parser, str(e))
    report = None
    post_cycle_results: list[PhaseResult] = []
    integrator_run_report = None
    try:
        report = run_dispatch_cycle(config)

        if config.apply:
            auth_error = None
            try:
                GitHubForge().check_auth()
            except ForgeAuthError as e:
                auth_error = e

            semantic_review_enabled = _decide_semantic_review_enabled()
            if semantic_review_enabled:
                r1 = _poll_pending_not_needed_reviews(
                    args, forge=config.forge, auth_error=auth_error
                )
                post_cycle_results.append(r1)
            r2 = _run_semantic_integrator(
                config, semantic_review_enabled, auth_error=auth_error
            )
            post_cycle_results.append(r2)
            integrator_run_report = r2.report
            if config.parent_issue_number is not None:
                r3 = _process_parent_completion(config, auth_error=auth_error)
                post_cycle_results.append(r3)
                r4 = _post_event_log_comment(config, report, auth_error=auth_error)
                post_cycle_results.append(r4)

        # 機械判定可能なレポート（標準出力のJSON）に後処理結果を統合する
        final_dict = _report_to_dict(report)
        final_dict["post_cycle_results"] = [res.to_dict() for res in post_cycle_results]
        print(json.dumps(final_dict, ensure_ascii=False, indent=2))

        summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary_path:
            write_github_step_summary(
                cycle_report=report,
                integrator_report=integrator_run_report,
                summary_path=summary_path,
                post_cycle_results=post_cycle_results,
            )
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    # 終了コードの決定
    exit_code = 0
    for res in post_cycle_results:
        if res.status == PhaseStatus.FATAL_FAILURE:
            exit_code = 1
        elif res.status == PhaseStatus.RETRYABLE_FAILURE and exit_code != 1:
            exit_code = 2

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
