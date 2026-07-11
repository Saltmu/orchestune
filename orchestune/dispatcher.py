from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
from pathlib import Path

# 実装コード自体はgithub.*を直接呼ばないが、tests/test_dispatcher.pyが
# orchestune.dispatcher.github.* をmock.patchの対象にしている（githubは共有モジュール
# オブジェクトのため、実処理がdispatch_cycle.py側にあっても同じモジュールにパッチが効く）。
# このimportを消すとそれらのパッチがAttributeErrorで壊れるため、意図的に保持する。
from orchestune import github  # noqa: F401
from orchestune.dispatch_cycle import (
    CycleReport,
    DispatcherConfig,
    _is_worktree_complete,
    _sync_external_locks,
    append_event_log,
    build_event_log_entry,
    run_dispatch_cycle,
)
from orchestune.dispatch_gc import (
    is_process_alive,
    remove_worktree,
    worktree_has_uncommitted_changes,
)
from orchestune.dispatch_locks import (
    ExternalLockScanResult,
    check_footprint_deviation,
    scan_external_locks,
)
from orchestune.dispatch_rebase import notify_force_serial, notify_recompute
from orchestune.dispatch_recovery import recover_run_state
from orchestune.dispatch_scoring import (
    Task,
    compute_priority_score,
    parse_task_from_issue,
    quota_available,
    select_next_tasks,
)
from orchestune.dispatch_state import (
    ActiveWorktree,
    CompletedWorktree,
    RunState,
    load_run_state,
    save_run_state,
)
from orchestune.dispatch_targets import (
    ClaudeCodeCloudRoutineDispatchTarget,
    DispatchHandle,
    DispatchTarget,
    LocalProcessDispatchTarget,
    build_dispatch_target,
    default_dry_run_command_builder,
)
from orchestune.dispatch_worktree import file_lock

__all__ = [
    "ActiveWorktree",
    "CompletedWorktree",
    "DispatchHandle",
    "DispatchTarget",
    "ExternalLockScanResult",
    "LocalProcessDispatchTarget",
    "RunState",
    "Task",
    "_is_worktree_complete",
    "_sync_external_locks",
    "append_event_log",
    "build_dispatch_target",
    "build_event_log_entry",
    "check_footprint_deviation",
    "compute_priority_score",
    "default_dry_run_command_builder",
    "file_lock",
    "is_process_alive",
    "load_run_state",
    "notify_force_serial",
    "notify_recompute",
    "parse_task_from_issue",
    "quota_available",
    "recover_run_state",
    "remove_worktree",
    "save_run_state",
    "scan_external_locks",
    "select_next_tasks",
    "worktree_has_uncommitted_changes",
]




def write_github_step_summary(
    cycle_report: CycleReport | None,
    integrator_report: dict | None,
    summary_path: str,
) -> None:
    lines = ["## 🤖 Orchestune Dispatch Summary\n"]

    if integrator_report:
        lines.append("### 🔍 仮マージ検証（Integrator）結果")
        status = integrator_report.get("status", "unknown")
        lines.append(f"全体ステータス: **{status}**\n")

        merged = integrator_report.get("merged", [])
        failed = integrator_report.get("failed", [])
        failed_reasons = integrator_report.get("failed_reasons", {})
        integration_pr_number = integrator_report.get("integration_pr_number")

        if not merged and not failed:
            lines.append("検証対象の完了タスク（`status:done`）はありませんでした。\n")
        else:
            lines.append("| サブタスクID | 結果 | 詳細 / 理由 |")
            lines.append("| --- | --- | --- |")
            for task_id in merged:
                lines.append(
                    f"| `{task_id}` | ✅ 成功 | 仮マージCI通過またはマージ済みスキップ |"
                )
            for task_id in failed:
                reason = failed_reasons.get(task_id, "不明なエラー")
                reason_short = reason.split("\n")[0]
                lines.append(f"| `{task_id}` | ❌ 失敗 | {reason_short} |")
            lines.append("")

        # Integratorの仕事は統合PRの作成までで、最終マージは常に人間が行うため、
        # そのPRへのリンクをサマリー上で必ず可視化する（run #68のように、成功していても
        # 誰にも気づかれず放置されるのを防ぐ）。
        if integration_pr_number:
            repo_slug = os.environ.get("GITHUB_REPOSITORY")
            pr_ref = (
                f"https://github.com/{repo_slug}/pull/{integration_pr_number}"
                if repo_slug
                else f"#{integration_pr_number}"
            )
            lines.append(
                f"➡️ **統合PR #{integration_pr_number}** が作成/検出されました。"
                f"最終マージには人間によるレビューが必要です: {pr_ref}\n"
            )

    if cycle_report:
        lines.append("### 🚀 新規起動タスク")
        if not cycle_report.selected:
            lines.append("今回新たに起動されたタスクはありません。\n")
        else:
            lines.append("| サブタスクID | Issue番号 | 優先度 |")
            lines.append("| --- | --- | --- |")
            for task in cycle_report.selected:
                lines.append(
                    f"| `{task.subtask_id}` | #{task.issue_number} | {task.priority} |"
                )
            lines.append("")

        lines.append("### 🔒 外部ロック（External Lock）変更")
        to_lock = cycle_report.lock_changes.get("to_lock", [])
        to_unlock = cycle_report.lock_changes.get("to_unlock", [])

        if not to_lock and not to_unlock:
            lines.append("外部ロックの変更はありませんでした。\n")
        else:
            lines.append("| サブタスクID | Issue番号 | アクション |")
            lines.append("| --- | --- | --- |")
            for task in to_lock:
                lines.append(
                    f"| `{task.subtask_id}` | #{task.issue_number} | 🔒 ロック付与 (`status:external-lock`) |"
                )
            for task in to_unlock:
                lines.append(
                    f"| `{task.subtask_id}` | #{task.issue_number} | 🔓 ロック解除 |"
                )
            lines.append("")

    try:
        with open(summary_path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except Exception as e:
        print(f"Warning: Failed to write to GITHUB_STEP_SUMMARY: {e}", file=sys.stderr)


def _report_to_dict(report: CycleReport) -> dict:
    return {
        "applied": report.applied,
        "quota_slots_available": report.quota_slots_available,
        "selected": [dataclasses.asdict(t) for t in report.selected],
        "lock_changes": {
            "to_lock": [dataclasses.asdict(t) for t in report.lock_changes["to_lock"]],
            "to_unlock": [
                dataclasses.asdict(t) for t in report.lock_changes["to_unlock"]
            ],
        },
        "deviation_events": report.deviation_events,
        "completion_events": report.completion_events,
        "promotion_events": report.promotion_events,
    }


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
        "--dispatch-target",
        choices=["local", "cloud-routine"],
        default="local",
        help="#215: エージェントの実ディスパッチ先。'cloud-routine'はClaude Codeクラウド"
        "ルーチンのfire APIへディスパッチする（要 --routine-id/--routine-token または"
        "ORCHESTUNE_ROUTINE_ID/ORCHESTUNE_ROUTINE_TOKEN環境変数）",
    )
    parser.add_argument(
        "--local-cmd",
        default=None,
        help="ローカルのCLI（agyなど）にディスパッチする際のコマンドテンプレート。"
        "例: 'agy --issue {issue_number}' や 'agy'。"
        "使用可能な変数: {issue_number}, {subtask_id}, {branch_name}, {worktree_path}",
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
        "--not-needed-review-state-path",
        type=Path,
        default=Path("not_needed_review_state.json"),
        help="#282: 保留中のstatus:not-needed検証レビュー（合否ポーリング・自動クローズ待ち）の永続化先",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

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
            args.dispatch_target,
            args.routine_id,
            args.routine_token,
            args.log_dir,
            local_cmd=args.local_cmd,
        ),
        deviation_buffer_lines=args.deviation_buffer_lines,
        max_recompute_retries=args.max_recompute_retries,
        not_needed_review_state_path=args.not_needed_review_state_path,
    )
    report = None
    integrator_run_report = None
    try:
        report = run_dispatch_cycle(config)
        print(json.dumps(_report_to_dict(report), ensure_ascii=False, indent=2))

        if config.apply:
            # 統合コーディネーターによる意味的レビュー（LLMによる統合PRのバグ検知、結果は
            # PRコメントのみで完結）と、#282のstatus:not-needed独立検証レビューの
            # 両方を、`ORCHESTUNE_SEMANTIC_REVIEW=0`でまとめて無効化できる。
            semantic_review_enabled = (
                os.environ.get("ORCHESTUNE_SEMANTIC_REVIEW", "1") != "0"
            )

            if semantic_review_enabled:
                # #282: status:not-needed判定の独立検証レビュー（保留分）のポーリング。
                try:
                    from orchestune.integration_coordinator import (
                        process_pending_not_needed_reviews,
                    )

                    not_needed_review_report = process_pending_not_needed_reviews(
                        args.not_needed_review_state_path
                    )
                    print("Pending Not-Needed Review Report:")
                    print(
                        json.dumps(
                            not_needed_review_report, ensure_ascii=False, indent=2
                        )
                    )
                except Exception as re:
                    print(
                        f"Warning: failed to process pending not-needed reviews: {re}",
                        file=sys.stderr,
                    )

            try:
                from orchestune.integrator import Integrator, IntegratorConfig

                integrator_config = IntegratorConfig(
                    parent_issue_number=config.parent_issue_number,
                    apply=config.apply,
                )
                # レビューはdispatcherと同一のクラウドルーチンを再利用して起動するため、
                # 実ディスパッチ先がクラウドルーチンのときのみ意味的レビューを有効化する。
                # レビューセッションは統合PRへコメントを残すのみで、自動マージ等の
                # 後続処理はPython側では一切行わない。
                if semantic_review_enabled and isinstance(
                    config.dispatch_target, ClaudeCodeCloudRoutineDispatchTarget
                ):
                    from orchestune.integration_coordinator import (
                        IntegrationCoordinator,
                    )

                    integrator_config.enable_semantic_review = True
                    integrator_config.coordinator = IntegrationCoordinator(
                        config.dispatch_target
                    )
                else:
                    integrator_config.enable_semantic_review = False
                integrator = Integrator(integrator_config)
                integrator_run_report = integrator.run()
                print("Integrator Report:")
                print(json.dumps(integrator_run_report, ensure_ascii=False, indent=2))
            except Exception as ie:
                print(f"Warning: Integrator failed to run: {ie}", file=sys.stderr)

        summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary_path:
            write_github_step_summary(
                cycle_report=report,
                integrator_report=integrator_run_report,
                summary_path=summary_path,
            )
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
