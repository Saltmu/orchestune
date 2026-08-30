"""GitHub Step Summary / JSONレポートの整形（読み取り専用、副作用はファイル出力のみ）。"""

from __future__ import annotations

import dataclasses
import os
import sys

from orchestune.consistency.supervisor import consistency_cycle_to_dict
from orchestune.dispatch.cycle import CycleReport
from orchestune.dispatch.result import PhaseResult, PhaseStatus
from orchestune.dispatch.scoring import decision_to_dict


def _format_post_cycle_summary(
    post_cycle_results: list[PhaseResult],
) -> list[str]:
    lines = [
        "### 🔄 後処理フェーズ結果（Post-Cycle Phases）",
        "| フェーズ名 | ステータス | 再試行要否 | 詳細 / エラー |",
        "| --- | --- | --- | --- |",
    ]
    for res in post_cycle_results:
        status_emoji = {
            PhaseStatus.SUCCESS: "✅ 成功",
            PhaseStatus.WARNING: "⚠️ 警告",
            PhaseStatus.RETRYABLE_FAILURE: "❌ 失敗（再試行可能）",
            PhaseStatus.FATAL_FAILURE: "🚨 致命的失敗",
        }.get(res.status, res.status.value)
        retry_text = "🔄 必要" if res.retryable else "-"
        detail = res.error_message if res.error_message else "-"
        lines.append(
            f"| `{res.phase_name}` | {status_emoji} | {retry_text} | {detail} |"
        )
    lines.append("")
    return lines


def _format_integrator_summary(integrator_report: dict) -> list[str]:
    lines = [
        "### 🔍 仮マージ検証（Integrator）結果",
        f"全体ステータス: **{integrator_report.get('status', 'unknown')}**\n",
    ]
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
            lines.append(f"| `{task_id}` | ❌ 失敗 | {reason.split(chr(10))[0]} |")
        lines.append("")

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
    return lines


def _format_scheduling_decisions(cycle_report: CycleReport) -> list[str]:
    """#660: 全候補の選定理由・rank・推定costを表として出す。

    起動されなかった候補こそ「なぜ選ばれなかったか」が運用上の関心事なので、
    選出分だけでなく候補全件を載せる。
    """
    if not cycle_report.scheduling_decisions:
        return []
    lines = [
        "### 🧮 スケジューリング判定（Scheduling Decisions）",
        f"モード: `{cycle_report.scheduling_decisions[0].mode}`\n",
        "| サブタスクID | Issue番号 | スコア | bottom level | 解放数 | rank精度 | 推定トークン | 結果 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for decision in cycle_report.scheduling_decisions:
        tokens = "-" if decision.estimated_tokens is None else decision.estimated_tokens
        rank_accuracy = (
            "cyclic-fallback"
            if not decision.exact_bottom_level
            else "downstream-fallback"
            if not decision.exact_downstream
            else "exact"
        )
        outcome = "✅ 起動" if decision.selected else f"⏸️ 見送り (`{decision.reason}`)"
        lines.append(
            f"| `{decision.subtask_id}` | #{decision.issue_number} | "
            f"{decision.score:.3f} | {decision.bottom_level:.0f} | "
            f"{decision.downstream_count} | {rank_accuracy} | {tokens} | {outcome} |"
        )
    lines.append("")
    return lines


def _format_cycle_report_summary(cycle_report: CycleReport) -> list[str]:
    lines = ["### 🚀 新規起動タスク"]
    if not cycle_report.selected:
        lines.append("今回新たに起動されたタスクはありません。\n")
    else:
        has_profiles = any(
            t.issue_number in cycle_report.execution_selections or t.execution_profile
            for t in cycle_report.selected
        )
        if has_profiles or cycle_report.execution_selections:
            lines.append(
                "| サブタスクID | Issue番号 | 優先度 | プロファイル | モデル |"
            )
            lines.append("| --- | --- | --- | --- | --- |")
            for task in cycle_report.selected:
                sel = cycle_report.execution_selections.get(task.issue_number)
                prof = sel.profile if sel else (task.execution_profile or "-")
                mdl = sel.model if (sel and sel.model) else "-"
                lines.append(
                    f"| `{task.subtask_id}` | #{task.issue_number} | {task.priority} | `{prof}` | `{mdl}` |"
                )
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
    return lines + _format_scheduling_decisions(cycle_report)


def write_github_step_summary(
    cycle_report: CycleReport | None,
    integrator_report: dict | None,
    summary_path: str,
    post_cycle_results: list[PhaseResult] | None = None,
) -> None:
    lines = ["## 🤖 Orchestune Dispatch Summary\n"]
    if post_cycle_results:
        lines.extend(_format_post_cycle_summary(post_cycle_results))
    if integrator_report:
        lines.extend(_format_integrator_summary(integrator_report))
    if cycle_report:
        lines.extend(_format_cycle_report_summary(cycle_report))

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
        "scheduling_decisions": [
            decision_to_dict(decision) for decision in report.scheduling_decisions
        ],
        "execution_selections": {
            str(issue): dataclasses.asdict(sel)
            for issue, sel in report.execution_selections.items()
        }
        if report.execution_selections
        else {},
        "consistency": consistency_cycle_to_dict(report.consistency),
    }
