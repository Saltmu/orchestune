"""#232: dispatch cycle後のベストエフォート後処理オーケストレーション。

`dispatch_cycle.run_dispatch_cycle`とは別の、`dispatcher.main()`から呼ばれる
ポストサイクルのワークフロー群（保留レビューのポーリング・意味的レビューを
含むIntegrator実行・親Issue完了処理）を集約する。いずれも失敗してもmain()の
続行を妨げないベストエフォート処理である。
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from pathlib import Path

from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.cycle import CycleReport
from orchestune.dispatch.result import PhaseResult, PhaseStatus
from orchestune.dispatch.summary import (
    merge_skips,
    render_forge_warnings_markdown,
    render_skipped_markdown,
)
from orchestune.dispatch.targets import ClaudeCodeCloudRoutineDispatchTarget
from orchestune.forge import Forge, ForgeAuthError
from orchestune.integrator import IntegrationStatus, Integrator, IntegratorConfig
from orchestune.integrator.coordinator import (
    DEFAULT_NOT_NEEDED_REVIEW_TIMEOUT_SECONDS,
    IntegrationCoordinator,
    process_pending_not_needed_reviews,
)
from orchestune.integrator.parent_completion import process_parent_completion


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
    state_path: Path,
    forge: Forge | None = None,
    auth_error: ForgeAuthError | None = None,
    timeout_seconds: float = DEFAULT_NOT_NEEDED_REVIEW_TIMEOUT_SECONDS,
) -> PhaseResult:
    """#282: status:not-needed判定の独立検証レビュー（保留分）をポーリングする。

    ベストエフォート処理: 失敗しても警告を出すだけでmain()は続行する。

    #512: レビュー合格でクローズされたIssueの回収回数（`task_reclaim_counts`）の
    破棄は、ここではなく次のディスパッチサイクルが行う——GitHub上でクローズ済みと
    確認できたIssueをまとめて落とす単一の規則（`dispatch_cycle_context`の
    `discard_reclaim_counts_for_closed_issues`）に集約している。

    #511: どちらの結果ラベルも付かないまま`timeout_seconds`を超えたエントリは、
    `process_pending_not_needed_reviews`が`status:blocked-human-review`へ
    終端させる（既定値は`config.not_needed_review_timeout_seconds`から伝播）。
    """

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


# Integratorパイプラインが成功として扱う唯一のステータス群（#207: これ以外は
# すべて失敗として扱うホワイトリスト方式。個別のエラーステータス追加時に
# 判定漏れが起きるブラックリスト方式を避けるため）。
_INTEGRATOR_SUCCESS_STATUSES = {
    IntegrationStatus.SUCCESS,
    IntegrationStatus.NO_DONE_TASKS,
}


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
    )
    if semantic_review_enabled and isinstance(
        config.dispatch_target, ClaudeCodeCloudRoutineDispatchTarget
    ):
        integrator_config.enable_semantic_review = True
        integrator_config.coordinator = IntegrationCoordinator(config.dispatch_target)
    else:
        integrator_config.enable_semantic_review = False
    return integrator_config


def _run_semantic_integrator(
    config: DispatcherConfig,
    semantic_review_enabled: bool,
    auth_error: ForgeAuthError | None = None,
) -> PhaseResult:
    def work() -> dict:
        integrator_config = _build_integrator_config(config, semantic_review_enabled)
        return dict(Integrator(integrator_config).run())

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


# footprint逸脱の判定（dispatch_rebase.py の _decide_footprint_deviation_outcome）が
# 返しうる`action`のうち、この2つは「何も新しく判断・遷移しなかった」ことを示す
# だけの結果である。`already_forced_serial`は、対象active worktreeが既に強制直列化
# 済みである限り、状態が変わらなくても毎サイクル同じイベントとして再生成され続ける
# （チャーン防止のための早期returnがそのままイベントにもなってしまうため）。
# これをそのまま「イベントあり」の判定材料にすると、空サイクルスキップのガードが
# 実質的に無力化され、force-serial状態が解消するまで毎サイクル同じ内容のコメントを
# 親Issueへ投稿し続けてしまう（#402レビュー指摘）。
_STEADY_STATE_DEVIATION_ACTIONS = frozenset(
    {"already_forced_serial", "skipped_unknown_subtask"}
)


def _noteworthy_deviation_events(report: CycleReport) -> list[dict]:
    """`report.deviation_events`のうち、定常状態の再通知ではないものだけを返す。"""
    return [
        event
        for event in report.deviation_events
        if event.get("action") not in _STEADY_STATE_DEVIATION_ACTIONS
    ]


def _format_completion_item(event: dict) -> str:
    issue_num = event.get("issue_number")
    subtask_id = event.get("subtask_id")
    action = event.get("action", "completed")
    usage = event.get("usage")

    if not (issue_num and subtask_id):
        return f"`{event}`"

    prefix = f"Issue #{issue_num}（`{subtask_id}`）"
    if usage and isinstance(usage, dict):
        model = usage.get("model") or "不明"
        tokens = usage.get("total_tokens")
        tokens_str = f"{tokens:,} tokens" if tokens is not None else "不明"
        return f"{prefix}: `{action}` [Model: `{model}`, Tokens: **{tokens_str}**]"
    return f"{prefix}: `{action}` [Model: `不明`, Tokens: **不明**]"


def _format_event_log_comment(report: CycleReport, deviation_events: list[dict]) -> str:
    lines = ["## 🤖 Orchestune Dispatch Cycle Report\n"]
    lines.append(f"Quota slots available: **{report.quota_slots_available}**\n")

    sections = [
        (
            "🚀 選定タスク（Selected）",
            [f"Issue #{t.issue_number}（`{t.subtask_id}`）" for t in report.selected],
        ),
        (
            "⚠️ footprint逸脱イベント（Deviation）",
            [f"`{event}`" for event in deviation_events],
        ),
        (
            "✅ 完了イベント（Completion）",
            [_format_completion_item(event) for event in report.completion_events],
        ),
        (
            "⬆️ 昇格イベント（Promotion）",
            [f"`{event}`" for event in report.promotion_events],
        ),
    ]
    for header, items in sections:
        if not items:
            continue
        lines.append(f"### {header}")
        lines.extend(f"- {item}" for item in items)
        lines.append("")

    # #787: 未選定タスクの有無は`_post_event_log_comment`の投稿要否
    # (`has_events`)には算入しない。未選定は毎サイクル起こりうるため、算入すると
    # 何も動いていないサイクルでも親Issueにコメントが積み上がる。
    lines.extend(
        render_skipped_markdown(merge_skips(report.skips, report.scheduling_decisions))
    )
    lines.extend(render_forge_warnings_markdown(report.forge_warnings))
    return "\n".join(lines)


def _post_event_log_comment(
    config: DispatcherConfig,
    report: CycleReport,
    auth_error: ForgeAuthError | None = None,
) -> PhaseResult:
    """#396: ディスパッチサイクルの意思決定ログを親Issueへコメント投稿する。

    `events.jsonl`はgitignore対象でCI環境では実行のたびに揮発するため、
    恒久的なトレーサビリティを補完する。GitHub Actions artifactは
    Actions固有機能でありローカル実行・Codex Cloud等では成立しないため、
    Orchestuneが全実行環境で共通して前提にできる`gh`（`Forge.add_comment`）
    経由で親Issueへ投稿する方式を採る（#396のコメント参照）。
    ベストエフォート処理: 失敗しても警告を出すだけでmain()は続行する。

    頻繁なディスパッチサイクルで親Issueが空コメントに埋め尽くされるのを
    防ぐため、選定・逸脱・完了・昇格のいずれのイベントも無いサイクルでは
    投稿をスキップする。footprint逸脱のうち、状態が変わらず定常的に再生成
    され続ける種類のイベント（`_noteworthy_deviation_events`参照）は、この
    判定・投稿内容のいずれからも除外する。
    """

    def work() -> dict:
        if config.parent_issue_number is None:
            return {"posted": False, "reason": "no parent issue configured"}

        deviation_events = _noteworthy_deviation_events(report)
        has_events = bool(
            report.selected
            or deviation_events
            or report.completion_events
            or report.promotion_events
        )
        if not has_events:
            return {"posted": False, "reason": "no events in this cycle"}

        body = _format_event_log_comment(report, deviation_events)
        config.resolved_forge.add_comment(config.parent_issue_number, body)
        return {"posted": True, "issue_number": config.parent_issue_number}

    return _run_best_effort_phase(
        phase_name="post_event_log_comment",
        report_label="Event Log Comment Report",
        work=work,
        auth_error=auth_error,
        auth_error_message="authentication failed while posting event log comment",
        failure_message="failed to post event log comment to parent issue",
    )


def _process_parent_completion(
    config: DispatcherConfig, auth_error: ForgeAuthError | None = None
) -> PhaseResult:
    """#170: 親Issue配下の全子Issue完了検知→最終PR用意、および最終PRの
    マージ検知→親Issueクローズを行う。ベストエフォート処理: 失敗しても警告を
    出すだけでmain()は続行する。
    """

    def work() -> dict:
        return process_parent_completion(
            config.parent_issue_number, config.apply, forge=config.forge
        )

    return _run_best_effort_phase(
        phase_name="process_parent_completion",
        report_label="Parent Completion Report",
        work=work,
        auth_error=auth_error,
        auth_error_message="authentication failed while processing parent completion",
        failure_message="failed to process parent completion",
    )
