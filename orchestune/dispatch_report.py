"""GitHub Step Summary / JSONレポートの整形（読み取り専用、副作用はファイル出力のみ）。"""

from __future__ import annotations

import dataclasses
import os
import sys

from orchestune.dispatch_cycle import CycleReport


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
