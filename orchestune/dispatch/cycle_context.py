"""Issue取得(GitHub)から、後続の各フェーズが読み取り専用で参照する
`CycleContext`の構築までを担うコーディネーター。"""

from __future__ import annotations

import sys
from dataclasses import dataclass

from orchestune.branch_naming import (
    build_task_branch_name,
    find_unique_matching_pr_branch,
)
from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.filters import _filter_by_parent
from orchestune.dispatch.phase_reconciliation import _dispatch_not_needed_review
from orchestune.dispatch.recovery import _extract_raw_subtask_id
from orchestune.dispatch.rules import CycleContext
from orchestune.dispatch.scoring import parse_task_from_issue
from orchestune.dispatch.state import RunState
from orchestune.issue_parsing import find_children_by_parent
from orchestune.labels import StatusLabel
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


# #512/PR#520レビュー8巡目対応(Codex P2): 台帳に残るIssueの状態を一括で解決する
# ためのラベル。`_fetch_issues`が取得しない終端ラベル——とりわけ本機能自身が付与する
# `status:blocked-human-review`——を1リクエストずつ問い合わせると、エスカレーション
# 済みのタスクが増えるほど毎サイクルのAPI呼び出しが線形に増えてしまう。
_LEDGER_BULK_LOOKUP_LABELS = (
    StatusLabel.BLOCKED_HUMAN_REVIEW,
    StatusLabel.MANUAL_MERGE_REQUIRED,
)

# #512/PR#520レビュー14巡目対応(Codex P2): 一括取得の取得件数と、1サイクルあたりの
# 個別問い合わせ件数の上限。台帳には件数上限を設けていない（未完了タスクの回数を
# 失わないため）ので、解決処理側でAPI呼び出し回数を有界に保つ。
_LEDGER_BULK_LOOKUP_MIN_LIMIT = 1000
_LEDGER_DIRECT_LOOKUPS_PER_CYCLE = 50


def _rotated_lookup_batch(run_state: RunState, unresolved: set[int]) -> list[int]:
    """個別問い合わせに回す記録を、サイクルをまたいで走査位置を進めながら選ぶ。

    PR#520レビュー15巡目対応(Codex P2): 常にIssue番号の若い順で先頭N件を見ると、
    それより後ろの記録は永久に確認されない（若い番号のIssueが開いたままなら、
    その後ろでクローズされた記録の回数がいつまでも台帳に残る）。

    同16巡目対応(Codex P2): 走査位置は壁時計ではなく`run_state`のカーソルで進める。
    `int(time.time()) % 件数`だと、一定周期で起動されるディスパッチャー
    （例: 300秒cron・未解決600件）では常に同じ2箇所しか見ないという組み合わせが
    生じ、間の記録が永久に取り残される。カーソルなら起動間隔に関わらず、
    高々`ceil(件数 / 1サイクルの上限)`サイクルで全件を一巡できる。

    ここで決めるのは「クローズ確認をどの順で行うか」だけで、ディスパッチの判断
    （起動対象・エスカレーションの有無）はこの順序に依存しない——確認できなかった
    記録は保持され、次サイクル以降で確認されるだけである。
    """
    ordered = sorted(unresolved)
    if len(ordered) <= _LEDGER_DIRECT_LOOKUPS_PER_CYCLE:
        run_state.task_reclaim_lookup_cursor = 0
        return ordered
    offset = run_state.task_reclaim_lookup_cursor % len(ordered)
    rotated = ordered[offset:] + ordered[:offset]
    batch = rotated[:_LEDGER_DIRECT_LOOKUPS_PER_CYCLE]
    run_state.task_reclaim_lookup_cursor = (offset + len(batch)) % len(ordered)
    return batch


def _resolve_bulk_label_states(
    unresolved: set[int], states: dict[int, str], config: DispatcherConfig
) -> set[int]:
    for label in _LEDGER_BULK_LOOKUP_LABELS:
        if not unresolved:
            return unresolved
        try:
            fetched = config.resolved_forge.list_issues_by_label(
                label,
                state="all",
                limit=max(_LEDGER_BULK_LOOKUP_MIN_LIMIT, len(unresolved) * 2),
            )
        except Exception as e:  # noqa: BLE001 - 解決できない分は次の手段へ
            print(
                f"Warning: could not list {label!r} issues while checking reclaim "
                f"counts: {e}",
                file=sys.stderr,
            )
            continue
        for issue in fetched:
            if issue.number in unresolved:
                states[issue.number] = issue.state.upper()
        unresolved -= set(states)
    return unresolved


def _resolve_direct_lookup_states(
    run_state: RunState,
    unresolved: set[int],
    states: dict[int, str],
    config: DispatcherConfig,
) -> None:
    for issue_number in _rotated_lookup_batch(run_state, unresolved):
        try:
            states[issue_number] = config.resolved_forge.get_issue_state(
                issue_number
            ).upper()
        except Exception as e:  # noqa: BLE001 - 確認できない記録は保持する
            print(
                f"Warning: could not check whether issue #{issue_number} is "
                f"closed; keeping its reclaim count: {e}",
                file=sys.stderr,
            )


def _resolve_ledger_issue_states(
    run_state: RunState, issues: IssuesByStatus, config: DispatcherConfig
) -> dict[int, str]:
    recorded = set(run_state.task_reclaim_counts)
    states = {
        issue.number: issue.state.upper()
        for issue in issues.all()
        if issue.number in recorded
    }
    unresolved = recorded - set(states)
    unresolved = _resolve_bulk_label_states(unresolved, states, config)
    _resolve_direct_lookup_states(run_state, unresolved, states, config)
    return states


def discard_reclaim_counts_for_closed_issues(
    run_state: RunState, issues: IssuesByStatus, config: DispatcherConfig
) -> list[int]:
    if not run_state.task_reclaim_counts:
        return []
    states = _resolve_ledger_issue_states(run_state, issues, config)
    removed: list[int] = []
    for issue_number in sorted(run_state.task_reclaim_counts):
        if states.get(issue_number) == "CLOSED":
            del run_state.task_reclaim_counts[issue_number]
            removed.append(issue_number)
    return removed


def _group_by_status(issues: list[IssueRecord]) -> IssuesByStatus:
    queued: list[IssueRecord] = []
    locked: list[IssueRecord] = []
    in_progress: list[IssueRecord] = []
    blocked: list[IssueRecord] = []
    done: list[IssueRecord] = []
    not_needed: list[IssueRecord] = []

    for issue in issues:
        is_open = issue.state == "OPEN"
        if is_open and StatusLabel.QUEUED in issue.labels:
            queued.append(issue)
        if is_open and StatusLabel.EXTERNAL_LOCK in issue.labels:
            locked.append(issue)
        if is_open and StatusLabel.IN_PROGRESS in issue.labels:
            in_progress.append(issue)
        if is_open and StatusLabel.BLOCKED in issue.labels:
            blocked.append(issue)
        if StatusLabel.DONE in issue.labels:
            done.append(issue)
        if StatusLabel.NOT_NEEDED in issue.labels:
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
    if config.parent_issue_number is not None:
        result = find_children_by_parent(
            config.resolved_forge, config.parent_issue_number
        )
        return _group_by_status(result.issues)

    return IssuesByStatus(
        queued=config.resolved_forge.list_issues_by_label(StatusLabel.QUEUED),
        locked=config.resolved_forge.list_issues_by_label(StatusLabel.EXTERNAL_LOCK),
        in_progress=config.resolved_forge.list_issues_by_label(StatusLabel.IN_PROGRESS),
        blocked=config.resolved_forge.list_issues_by_label(StatusLabel.BLOCKED),
        done=config.resolved_forge.list_issues_by_label(StatusLabel.DONE, state="all"),
        not_needed=config.resolved_forge.list_issues_by_label(
            StatusLabel.NOT_NEEDED, state="all"
        ),
    )


def _build_task_mappings(
    all_issues: list[IssueRecord],
) -> tuple[dict, dict, set[str]]:
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
    done_subtask_ids = {
        task.subtask_id
        for task in tasks_by_issue.values()
        if StatusLabel.DONE in task.status_labels and task.subtask_id
    }
    return tasks_by_issue, issue_number_by_subtask_id, done_subtask_ids


def _build_pr_mappings(tasks_by_issue: dict, prs: list) -> tuple[dict, set, set, dict]:
    """#777 Codexレビュー(Round4): `subtask_branch_map`は
    `dispatch/rebase.py`の自動リベースで実際のrebase対象ブランチとして
    使われるため、canonical名限定のままでは非デフォルトprefixの依存先PR
    （例: `codex/issue-N-a`）がCI成功していても検出できず、依存元タスクが
    誤ってブロックされ続ける。recovery/integrationと同じ①canonical完全一致
    →②厳密単一PR一致（fork安全）の順で実際のPRブランチを解決する。
    """
    pr_by_branch = {pr.head_ref: pr for pr in prs}
    ci_passed_pr_subtask_ids = set()
    changes_requested_subtask_ids = set()
    subtask_branch_map = {}

    for task in tasks_by_issue.values():
        if not task.subtask_id:
            continue
        canonical_branch = build_task_branch_name(task.issue_number, task.subtask_id)
        pr = pr_by_branch.get(canonical_branch)
        branch_name = canonical_branch
        if pr is None:
            fallback_branch = find_unique_matching_pr_branch(
                prs, task.issue_number, task.subtask_id
            )
            if fallback_branch is not None:
                branch_name = fallback_branch
                pr = pr_by_branch.get(fallback_branch)

        subtask_branch_map[task.subtask_id] = branch_name

        if pr:
            if pr.review_decision == "CHANGES_REQUESTED":
                changes_requested_subtask_ids.add(task.subtask_id)
            elif pr.is_ci_passing:
                ci_passed_pr_subtask_ids.add(task.subtask_id)

    return (
        pr_by_branch,
        ci_passed_pr_subtask_ids,
        changes_requested_subtask_ids,
        subtask_branch_map,
    )


def _build_cycle_context(
    issues: IssuesByStatus,
    run_state: RunState,
    config: DispatcherConfig,
) -> CycleContext:
    all_issues = issues.all()
    (
        tasks_by_issue,
        issue_number_by_subtask_id,
        done_subtask_ids,
    ) = _build_task_mappings(all_issues)

    prs = config.resolved_forge.list_open_prs(paginate_files=True)
    (
        pr_by_branch,
        ci_passed_pr_subtask_ids,
        changes_requested_subtask_ids,
        subtask_branch_map,
    ) = _build_pr_mappings(tasks_by_issue, prs)

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
