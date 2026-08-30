from __future__ import annotations

import sys
from collections.abc import Sequence

from orchestune.forge import Forge, GitHubForge
from orchestune.integrator.final_pr_body import (
    ChildSummary,
    collect_child_summaries,
    render_final_pr_body,
)
from orchestune.issue_parsing import find_children_by_parent
from orchestune.labels import StatusLabel
from orchestune.models import IssueRecord, PrRecord, Task

# #295: GitHubコメントの肥大化を避けるため、末尾のみを埋め込む。
# エラーメッセージ本体は通常出力の末尾に現れるため、これで十分な情報量を確保する。
CI_OUTPUT_COMMENT_TAIL_CHARS = 4000


def _is_reusable_integration_pr(pr: PrRecord, head: str, base: str) -> bool:
    """#243: head名だけの照合では、外部fork・別baseの同名branch PRを正規統合PRと
    誤認し、parent modeでは未検証のPRを自動マージし得る。upstream repository上の
    正規head（`is_cross_repository is False`）かつ指定base向けのPRだけを再利用し、
    identityを確認できないPR（`base_ref`空・`is_cross_repository`不明）は
    fail closedで再利用しない。"""
    return (
        pr.head_ref == head and pr.base_ref == base and pr.is_cross_repository is False
    )


def ensure_integration_pr(
    temp_branch: str,
    base_branch: str,
    merged_tasks: list[str],
    forge: Forge | None = None,
) -> int | None:
    """統合ブランチ(`temp_branch`)から`base_branch`へのPRを作成/再利用する。

    既にopenなPRがあれば重複作成せずその番号を返す。PR作成自体に失敗しても
    （差分無し等）Integrator全体は失敗させず、警告ログのみ出して`None`を返す。
    """
    forge = forge or GitHubForge()
    try:
        base = base_branch.removeprefix("origin/")
        title, body = _integration_pr_content(base, merged_tasks)
        existing = _find_reusable_integration_pr(forge, temp_branch, base)
        if existing is not None:
            _update_reused_integration_pr(forge, existing.number, title, body)
            return existing.number
        return forge.create_pull_request(
            head=temp_branch, base=base, title=title, body=body
        )
    except Exception as e:
        print(f"Warning: Failed to ensure integration PR: {e}", file=sys.stderr)
        return None


def _integration_pr_content(base: str, merged_tasks: list[str]) -> tuple[str, str]:
    merge_note = (
        "このPRはOrchestune Integratorが自動的にマージし、対象Issueも自動的にクローズします。"
        if base.startswith("parent/")
        else "最終マージは人間が行ってください。"
    )
    tasks = ", ".join(merged_tasks)
    return f"Integrate completed tasks ({tasks})", (
        "Orchestune Integrator が仮マージCI通過後に作成した統合PRです。\n"
        f"統合済みタスク: {tasks}\n\n{merge_note}"
    )


def _find_reusable_integration_pr(
    forge: Forge, head: str, base: str
) -> PrRecord | None:
    return next(
        (
            pr
            for pr in forge.list_open_prs()
            if _is_reusable_integration_pr(pr, head, base)
        ),
        None,
    )


def _update_reused_integration_pr(
    forge: Forge, number: int, title: str, body: str
) -> None:
    try:
        forge.update_pull_request(number, title=title, body=body)
    except Exception as error:
        print(
            f"Warning: Failed to update integration PR #{number}: {error}",
            file=sys.stderr,
        )


def ensure_parent_final_pr(
    parent_issue_number: int,
    base_branch: str = "main",
    forge: Forge | None = None,
    children: Sequence[IssueRecord] | None = None,
) -> int | None:
    """#170: 親Issue配下の全子Issueが完了した際、`parent/issue-{N}`から
    `base_branch`への最終統合PRを用意する。

    このPRのマージが「最終マージ」であり、常に人間が行う。マージ検知後の
    親Issueクローズは`parent_completion.process_parent_completion`が担う。

    #681: 本文には`Closes #`と子Issue・サブタスクPR・レビュー結果の一覧を
    埋め込む（`final_pr_body`）。`children`は呼び出し元が既に取得済みの子Issue
    で、`find_children_by_parent`の二重呼び出しを避けるために受け取る。
    """
    forge = forge or GitHubForge()
    try:
        head = f"parent/issue-{parent_issue_number}"
        title = f"Integrate parent issue #{parent_issue_number} into {base_branch}"
        summaries = _final_pr_summaries(forge, parent_issue_number, children)
        body = render_final_pr_body(parent_issue_number, summaries)

        existing = _find_reusable_integration_pr(forge, head, base_branch)
        if existing is not None:
            # #375と同じく再利用時も本文を最新化する。ただし一覧を1件も作れな
            # かった縮退サイクルでは書き換えない: 一時的なAPI障害で、投稿済み
            # の正しい一覧を失わせないため（fail closed）。
            # なおタイトルは生成時と同一の決定論的な文字列であり、`Forge`が
            # 本文単独の更新を公開していないため毎回一緒に送る（人間がPRを
            # リネームしていた場合は元の表記へ戻る）。
            if summaries:
                _update_reused_integration_pr(forge, existing.number, title, body)
            return existing.number

        return forge.create_pull_request(
            head=head, base=base_branch, title=title, body=body
        )
    except Exception as e:
        print(f"Warning: Failed to ensure parent final PR: {e}", file=sys.stderr)
        return None


def _final_pr_summaries(
    forge: Forge,
    parent_issue_number: int,
    children: Sequence[IssueRecord] | None,
) -> list[ChildSummary]:
    """一覧テーブルの行を用意する。解決できなければ空リスト（テーブル省略）。

    子Issueの探索に失敗しても最終PRの確保自体は諦めない。一覧が欠けることより、
    人間がマージすべき最終PRが存在しないことのほうが重い。
    """
    if children is None:
        try:
            children = find_children_by_parent(forge, parent_issue_number).issues
        except Exception as error:
            print(
                f"Warning: Failed to discover children of #{parent_issue_number} "
                f"while building the final integration PR body: {error}",
                file=sys.stderr,
            )
            return []
    return collect_child_summaries(forge, parent_issue_number, children)


def handle_merge_failure(
    task: Task,
    reason: str,
    apply: bool,
    ci_output: str | None = None,
    forge: Forge | None = None,
) -> None:
    if ci_output:
        # #295: ジョブログ（stderr）には切り詰めずに全文を残し、
        # コメントに書ききれない詳細もそこから追跡できるようにする。
        print(
            f"[Integrator] CI failure output for {task.subtask_id}:\n{ci_output}",
            file=sys.stderr,
        )
    if apply:
        forge = forge or GitHubForge()
        # #254: remove→addの順だと、removeが成功した直後にaddが一時障害で
        # 例外を送出した場合、Issueがどのprimary status(`status:done`/
        # `status:queued`)にも属さなくなり、dispatcher/Integrator両方の
        # 検索対象から恒久的に脱落する。add→removeの順にすることで、
        # 途中で例外が起きても必ずどちらか一方のラベルが残る:
        # - addが失敗: status:doneが残り、次cycleのIntegratorが
        #   同じ done タスクとして再検出し、この関数を再試行する。
        # - addは成功しremoveが失敗: status:queued/status:doneが
        #   一時的に両方付いた状態になるが、Issueが検索対象から
        #   消えることはなく、次cycleでremoveが再試行される。
        forge.add_label(task.issue_number, StatusLabel.QUEUED)
        forge.remove_label(task.issue_number, StatusLabel.DONE)
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
        forge.add_comment(task.issue_number, comment_body)
