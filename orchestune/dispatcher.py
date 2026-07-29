from __future__ import annotations

import argparse
import json
import os
import sys
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any, NoReturn, cast

from orchestune.dispatch_config import DispatcherConfig
from orchestune.dispatch_cycle import run_dispatch_cycle
from orchestune.dispatch_report import _report_to_dict, write_github_step_summary
from orchestune.dispatch_result import PhaseResult, PhaseStatus
from orchestune.dispatch_targets import (
    ClaudeCodeCloudRoutineDispatchTarget,
    build_dispatch_target,
    resolve_default_dispatch_target_name,
)
from orchestune.forge import ForgeAuthError, GitHubForge
from orchestune.integration_coordinator import (
    IntegrationCoordinator,
    process_pending_not_needed_reviews,
)
from orchestune.integrator import Integrator, IntegratorConfig
from orchestune.parent_completion import process_parent_completion


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
    return parser


def _decide_semantic_review_enabled() -> bool:
    """統合コーディネーターによる意味的レビュー（LLMによる統合PRのバグ検知、結果は
    PRコメントのみで完結）と、#282のstatus:not-needed独立検証レビューの両方を、
    `ORCHESTUNE_SEMANTIC_REVIEW=0`でまとめて無効化できる。"""
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
    """#232: main()から呼ばれるベストエフォート後処理フェーズの共通実行部。

    `_poll_pending_not_needed_reviews`・`_run_semantic_integrator`・
    `_process_parent_completion`に重複していたtry/exceptボイラープレート
    （`ForgeAuthError`ならFATAL_FAILURE、その他例外ならRETRYABLE_FAILURE）を
    集約する。失敗してもここでは例外を投げず、`PhaseResult`として返すだけで
    main()の続行を妨げない。
    """
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
            phase_name=phase_name, status=status, report=report, retryable=retryable
        )
    except ForgeAuthError as e:
        print(f"Error: {auth_error_message}: {e}", file=sys.stderr)
        return PhaseResult(
            phase_name=phase_name,
            status=PhaseStatus.FATAL_FAILURE,
            error_message=str(e),
            retryable=False,
        )
    except Exception as e:
        print(f"Warning: {failure_message}: {e}", file=sys.stderr)
        return PhaseResult(
            phase_name=phase_name,
            status=PhaseStatus.RETRYABLE_FAILURE,
            error_message=str(e),
            retryable=True,
        )


def _poll_pending_not_needed_reviews(
    args: argparse.Namespace, auth_error: ForgeAuthError | None = None
) -> PhaseResult:
    """#282: status:not-needed判定の独立検証レビュー（保留分）をポーリングする。

    ベストエフォート処理: 失敗しても警告を出すだけでmain()は続行する。
    """

    def work() -> dict:
        return process_pending_not_needed_reviews(args.not_needed_review_state_path)

    return _run_best_effort_phase(
        phase_name="poll_pending_not_needed_reviews",
        report_label="Pending Not-Needed Review Report",
        work=work,
        auth_error=auth_error,
        auth_error_message="authentication failed while polling reviews",
        failure_message="failed to process pending not-needed reviews",
    )


# Integratorパイプラインが成功として扱う唯一のステータス群（#207: これ以外は
# すべて失敗として扱うホワイトリスト方式。個別のエラーステータス追加時に
# 判定漏れが起きるブラックリスト方式を避けるため）。
_INTEGRATOR_SUCCESS_STATUSES = {"success", "no_done_tasks"}


def _run_semantic_integrator(
    config: DispatcherConfig,
    semantic_review_enabled: bool,
    auth_error: ForgeAuthError | None = None,
) -> PhaseResult:
    """統合コーディネーターによる意味的レビューを含め、Integratorを実行する。

    レビューはdispatcherと同一のクラウドルーチンを再利用して起動するため、
    実ディスパッチ先がクラウドルーチンのときのみ意味的レビューを有効化する。
    レビューセッションは統合PRへコメントを残すのみで、自動マージ等の後続処理は
    Python側では一切行わない。ベストエフォート処理: 失敗しても警告を出すだけで
    main()は続行する。
    """

    def work() -> dict:
        integrator_config = IntegratorConfig(
            parent_issue_number=config.parent_issue_number,
            apply=config.apply,
        )
        if semantic_review_enabled and isinstance(
            config.dispatch_target, ClaudeCodeCloudRoutineDispatchTarget
        ):
            integrator_config.enable_semantic_review = True
            integrator_config.coordinator = IntegrationCoordinator(
                config.dispatch_target
            )
        else:
            integrator_config.enable_semantic_review = False
        return Integrator(integrator_config).run()

    def evaluate_report(report: dict) -> tuple[PhaseStatus, bool]:
        if report.get("status") not in _INTEGRATOR_SUCCESS_STATUSES:
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


def _process_parent_completion(
    config: DispatcherConfig, auth_error: ForgeAuthError | None = None
) -> PhaseResult:
    """#170: 親Issue配下の全子Issue完了検知→最終PR用意、および最終PRの
    マージ検知→親Issueクローズを行う。ベストエフォート処理: 失敗しても警告を
    出すだけでmain()は続行する。
    """

    def work() -> dict:
        return process_parent_completion(config.parent_issue_number, config.apply)

    return _run_best_effort_phase(
        phase_name="process_parent_completion",
        report_label="Parent Completion Report",
        work=work,
        auth_error=auth_error,
        auth_error_message="authentication failed while processing parent completion",
        failure_message="failed to process parent completion",
    )


def load_config_file(cwd: Path | None = None) -> dict[str, Any]:
    """Load the first dispatcher configuration file found in *cwd*.

    Configuration syntax errors are deliberately fatal to the caller: falling
    through to another file or to CLI defaults would make a misspelled setting
    look like a successful dispatch.
    """
    if cwd is None:
        cwd = Path.cwd()

    orchestune_toml = cwd / "orchestune.toml"
    if orchestune_toml.exists():
        try:
            with open(orchestune_toml, "rb") as f:
                return cast(dict[str, Any], tomllib.load(f))
        except (OSError, tomllib.TOMLDecodeError) as e:
            raise ValueError(f"failed to load {orchestune_toml}: {e}") from e

    pyproject_toml = cwd / "pyproject.toml"
    if pyproject_toml.exists():
        try:
            with open(pyproject_toml, "rb") as f:
                data = tomllib.load(f)
                config = data.get("tool", {}).get("orchestune", {})
                if not isinstance(config, dict):
                    raise ValueError(
                        f"{pyproject_toml}: [tool.orchestune] must be a table"
                    )
                return cast(dict[str, Any], config)
        except (OSError, tomllib.TOMLDecodeError) as e:
            raise ValueError(f"failed to load {pyproject_toml}: {e}") from e

    return {}


def _config_error(parser: argparse.ArgumentParser, message: str) -> NoReturn:
    parser.error(f"invalid dispatcher config: {message}")


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
        normalized_key = key.replace("-", "_")
        if normalized_key == "parent_issue_number":
            normalized_key = "parent_issue"
        action = actions.get(normalized_key)
        if action is None or normalized_key == "help":
            _config_error(parser, f"unknown key {key!r}")

        if normalized_key in {"apply", "zombie_gc"}:
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

    args = parser.parse_args(argv)
    dispatch_target_name = args.dispatch_target or resolve_default_dispatch_target_name(
        os.environ
    )

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
        ),
        deviation_buffer_lines=args.deviation_buffer_lines,
        max_recompute_retries=args.max_recompute_retries,
        task_timeout_seconds=args.task_timeout_seconds,
        zombie_gc=args.zombie_gc,
        not_needed_review_state_path=args.not_needed_review_state_path,
    )
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
                r1 = _poll_pending_not_needed_reviews(args, auth_error=auth_error)
                post_cycle_results.append(r1)
            r2 = _run_semantic_integrator(
                config, semantic_review_enabled, auth_error=auth_error
            )
            post_cycle_results.append(r2)
            integrator_run_report = r2.report
            if config.parent_issue_number is not None:
                r3 = _process_parent_completion(config, auth_error=auth_error)
                post_cycle_results.append(r3)

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
