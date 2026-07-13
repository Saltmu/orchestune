from __future__ import annotations

import sys

from orchestune import github
from orchestune.dispatcher import Task

# #295: GitHubコメントの肥大化を避けるため、末尾のみを埋め込む。
# エラーメッセージ本体は通常出力の末尾に現れるため、これで十分な情報量を確保する。
CI_OUTPUT_COMMENT_TAIL_CHARS = 4000


def ensure_integration_pr(
    temp_branch: str, base_branch: str, merged_tasks: list[str]
) -> int | None:
    """統合ブランチ(`temp_branch`)から`base_branch`へのPRを作成/再利用する。

    既にopenなPRがあれば重複作成せずその番号を返す。PR作成自体に失敗しても
    （差分無し等）Integrator全体は失敗させず、警告ログのみ出して`None`を返す。
    """
    try:
        existing = [pr for pr in github.list_open_prs() if pr.head_ref == temp_branch]
        if existing:
            return existing[0].number

        base = base_branch.removeprefix("origin/")
        if base.startswith("parent/"):
            merge_note = (
                "このPRはOrchestune Integratorが自動的にマージし、"
                "対象Issueも自動的にクローズします。"
            )
        else:
            merge_note = "最終マージは人間が行ってください。"
        return github.create_pull_request(
            head=temp_branch,
            base=base,
            title=f"Integrate completed tasks ({', '.join(merged_tasks)})",
            body=(
                "Orchestune Integrator が仮マージCI通過後に作成した統合PRです。\n"
                f"統合済みタスク: {', '.join(merged_tasks)}\n\n"
                f"{merge_note}"
            ),
        )
    except Exception as e:
        print(f"Warning: Failed to ensure integration PR: {e}", file=sys.stderr)
        return None


def ensure_parent_final_pr(
    parent_issue_number: int, base_branch: str = "main"
) -> int | None:
    """#170: 親Issue配下の全子Issueが完了した際、`parent/issue-{N}`から
    `base_branch`への最終統合PRを用意する。

    このPRのマージが「最終マージ」であり、常に人間が行う。マージ検知後の
    親Issueクローズは`parent_completion.process_parent_completion`が担う。
    """
    try:
        head = f"parent/issue-{parent_issue_number}"
        existing = [pr for pr in github.list_open_prs() if pr.head_ref == head]
        if existing:
            return existing[0].number

        return github.create_pull_request(
            head=head,
            base=base_branch,
            title=f"Integrate parent issue #{parent_issue_number} into {base_branch}",
            body=(
                f"親Issue #{parent_issue_number} 配下の全子Issueが完了したため、"
                "Orchestune Integratorが作成した最終統合PRです。\n\n"
                "このPRのマージが最終マージです。人間がレビューの上マージして"
                "ください。マージが検知され次第、Orchestuneが親Issueを"
                "自動的にクローズします。"
            ),
        )
    except Exception as e:
        print(f"Warning: Failed to ensure parent final PR: {e}", file=sys.stderr)
        return None


def handle_merge_failure(
    task: Task, reason: str, apply: bool, ci_output: str | None = None
) -> None:
    if ci_output:
        # #295: ジョブログ（stderr）には切り詰めずに全文を残し、
        # コメントに書ききれない詳細もそこから追跡できるようにする。
        print(
            f"[Integrator] CI failure output for {task.subtask_id}:\n{ci_output}",
            file=sys.stderr,
        )
    if apply:
        github.remove_label(task.issue_number, "status:done")
        github.add_label(task.issue_number, "status:queued")
        comment_body = (
            f"仮マージCIでエラーが検出されたため、マージを取り消し差し戻しました。\n"
            f"理由: {reason}\n"
        )
        if ci_output:
            truncated = ci_output[-CI_OUTPUT_COMMENT_TAIL_CHARS:]
            comment_body += (
                "\n<details><summary>CI出力（末尾"
                f"{CI_OUTPUT_COMMENT_TAIL_CHARS}文字）</summary>\n\n"
                f"```\n{truncated}\n```\n</details>\n"
            )
        comment_body += "自動修復エージェントの再起動を待ちます。"
        github.add_comment(task.issue_number, comment_body)
