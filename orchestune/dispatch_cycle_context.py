"""Issue取得(GitHub)から、後続の各フェーズが読み取り専用で参照する
`CycleContext`の構築までを担うコーディネーター。"""

from __future__ import annotations

from dataclasses import dataclass

from orchestune.dispatch_config import DispatcherConfig
from orchestune.dispatch_filters import _filter_by_parent
from orchestune.dispatch_phase_reconciliation import _dispatch_not_needed_review
from orchestune.dispatch_recovery import _extract_raw_subtask_id
from orchestune.dispatch_rules import CycleContext
from orchestune.dispatch_scoring import parse_task_from_issue
from orchestune.dispatch_state import RunState
from orchestune.issue_parsing import find_children_by_parent
from orchestune.models import IssueRecord


@dataclass
class IssuesByStatus:
    """ステータスラベル別に取得したIssueの束。

    Issue取得直後は`list[IssueRecord]`が6個ばらばらのローカル変数になりがちで、
    後段で似た名前の変数を取り違えるミスを誘発しやすいため、1つの型にまとめる。
    """

    queued: list[IssueRecord]
    locked: list[IssueRecord]
    in_progress: list[IssueRecord]
    blocked: list[IssueRecord]
    done: list[IssueRecord]
    not_needed: list[IssueRecord]

    def all(self) -> list[IssueRecord]:
        return [
            *self.queued,
            *self.locked,
            *self.in_progress,
            *self.blocked,
            *self.done,
            *self.not_needed,
        ]

    def filtered_by_parent(self, parent_issue_number: int | None) -> IssuesByStatus:
        """`parent_issue_number`が指定されている場合、親Issueが一致する子Issueのみに絞る。"""
        return IssuesByStatus(
            queued=_filter_by_parent(self.queued, parent_issue_number),
            locked=_filter_by_parent(self.locked, parent_issue_number),
            in_progress=_filter_by_parent(self.in_progress, parent_issue_number),
            blocked=_filter_by_parent(self.blocked, parent_issue_number),
            done=_filter_by_parent(self.done, parent_issue_number),
            not_needed=_filter_by_parent(self.not_needed, parent_issue_number),
        )


def discard_reclaim_counts_for_closed_issues(
    run_state: RunState, issues: IssuesByStatus
) -> list[int]:
    """#512: GitHub上でクローズ済みと確認できたIssueの回収回数を台帳から破棄する。

    PR#520レビュー6巡目対応(Codex P2): 破棄の根拠は「Issueが閉じたこと」ただ一つに
    統一する。`_finalize_completed_worktree`が完了を検知した時点（`status:done`）で
    破棄していたが、その時点ではIssueはまだ開いており、Integratorの仮マージCIが
    失敗すれば`handle_merge_failure`が同じIssueを`status:queued`へ差し戻す。
    そこで回数が0に戻っていると、「GC回収 → ワーカー完了 → 統合失敗」の繰り返しで
    `max_task_reclaims`を素通りできてしまう。Issueがクローズされていれば、そのタスクが
    再び起動されることはない（人間が再オープンした場合は新しい実行として数え直す）。

    この判定はGitHubを真実として毎サイクル導出し直すため、破棄をディスクへ即時
    永続化する必要はない（保存前に落ちても次サイクルで同じ結論に到達する）。
    逆に、取得できたIssueの中で**クローズが確認できたものだけ**を破棄対象とするため、
    Issue取得が部分的だったり、`--parent-issue`で対象が絞られていたり、
    `status:blocked-human-review`のように取得対象ラベルに含まれない状態であっても、
    未完了タスクのカウンタを誤って落とすことはない。
    """
    closed_issue_numbers = {
        issue.number for issue in issues.all() if issue.state.upper() == "CLOSED"
    }
    return [
        issue_number
        for issue_number in sorted(closed_issue_numbers)
        if run_state.task_reclaim_counts.pop(issue_number, None) is not None
    ]


def _group_by_status(issues: list[IssueRecord]) -> IssuesByStatus:
    """#156: `forge.list_sub_issues`が返す親Issue配下の全Issueを、
    `list_issues_by_label`のstate引数（open/all）と同じ意味論でステータス
    ラベル別に分類する（`status:done`/`status:not-needed`はclosedも含める）。"""
    queued: list[IssueRecord] = []
    locked: list[IssueRecord] = []
    in_progress: list[IssueRecord] = []
    blocked: list[IssueRecord] = []
    done: list[IssueRecord] = []
    not_needed: list[IssueRecord] = []

    for issue in issues:
        is_open = issue.state == "OPEN"
        if is_open and "status:queued" in issue.labels:
            queued.append(issue)
        if is_open and "status:external-lock" in issue.labels:
            locked.append(issue)
        if is_open and "status:in-progress" in issue.labels:
            in_progress.append(issue)
        if is_open and "status:blocked" in issue.labels:
            blocked.append(issue)
        if "status:done" in issue.labels:
            done.append(issue)
        if "status:not-needed" in issue.labels:
            not_needed.append(issue)

    return IssuesByStatus(
        queued=queued,
        locked=locked,
        in_progress=in_progress,
        blocked=blocked,
        done=done,
        not_needed=not_needed,
    )


def _fetch_issues(config: DispatcherConfig) -> IssuesByStatus:
    """ステータスラベルごとにIssueをGitHubから取得する。

    #156: `config.parent_issue_number`が指定されている場合、無関係な親配下の
    Issueまでリポジトリ全体から取得して後段で破棄する無駄を避けるため、
    `forge.list_sub_issues`による親Issue起点のfast pathを使う。
    """
    if config.parent_issue_number is not None:
        result = find_children_by_parent(
            config.resolved_forge, config.parent_issue_number
        )
        return _group_by_status(result.issues)

    return IssuesByStatus(
        queued=config.resolved_forge.list_issues_by_label("status:queued"),
        locked=config.resolved_forge.list_issues_by_label("status:external-lock"),
        in_progress=config.resolved_forge.list_issues_by_label("status:in-progress"),
        blocked=config.resolved_forge.list_issues_by_label("status:blocked"),
        # #236: 完了Issueは人間が通常のGitHub運用でCloseすることが多いため、
        # 依存解決判定はclosedなIssueも含めて検索する。
        done=config.resolved_forge.list_issues_by_label("status:done", state="all"),
        # #280: セッションがstatus:not-neededを付与すると同時にstatus:in-progressを
        # 外すため、in_progress側の一覧には現れなくなる。tasks_by_issueに含めて
        # おかないと_process_active_worktrees側で完了検知できず、依存解決からも
        # 漏れてしまう（closedなIssueもクローズ後の依存解決に必要なためstate="all"）。
        not_needed=config.resolved_forge.list_issues_by_label(
            "status:not-needed", state="all"
        ),
    )


def _build_cycle_context(
    issues: IssuesByStatus,
    run_state: RunState,
    config: DispatcherConfig,
) -> CycleContext:
    """取得済みIssue群から、後続の各ステージが読み取り専用で参照する
    `CycleContext`を組み立てる。"""
    all_issues = issues.all()

    issue_to_subtask_id: dict[int, str] = {}
    for issue in all_issues:
        sub_id = _extract_raw_subtask_id(issue)
        if sub_id:
            issue_to_subtask_id[issue.number] = sub_id

    tasks_by_issue = {
        issue.number: parse_task_from_issue(issue, issue_to_subtask_id)
        for issue in all_issues
    }
    issue_number_by_subtask_id = {
        task.subtask_id: task.issue_number
        for task in tasks_by_issue.values()
        if task.subtask_id
    }

    prs = config.resolved_forge.list_open_prs(paginate_files=True)

    done_subtask_ids = {
        task.subtask_id
        for task in tasks_by_issue.values()
        if "status:done" in task.status_labels and task.subtask_id
    }

    pr_by_branch = {pr.head_ref: pr for pr in prs}
    ci_passed_pr_subtask_ids = set()
    changes_requested_subtask_ids = set()
    subtask_branch_map = {}

    for task in tasks_by_issue.values():
        if not task.subtask_id:
            continue
        branch_name = f"claude/issue-{task.issue_number}-{task.subtask_id}"
        subtask_branch_map[task.subtask_id] = branch_name

        pr = pr_by_branch.get(branch_name)
        if pr:
            if pr.review_decision == "CHANGES_REQUESTED":
                changes_requested_subtask_ids.add(task.subtask_id)
            elif pr.is_ci_passing:
                ci_passed_pr_subtask_ids.add(task.subtask_id)

    return CycleContext(
        run_state=run_state,
        tasks_by_issue=tasks_by_issue,
        issue_number_by_subtask_id=issue_number_by_subtask_id,
        done_subtask_ids=done_subtask_ids,
        ci_passed_pr_subtask_ids=ci_passed_pr_subtask_ids,
        changes_requested_subtask_ids=changes_requested_subtask_ids,
        subtask_branch_map=subtask_branch_map,
        prs=prs,
        pr_by_branch=pr_by_branch,
        config=config,
        not_needed_review_dispatcher=_dispatch_not_needed_review,
    )
