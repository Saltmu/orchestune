"""1サイクル分のディスパッチオーケストレーション本体。"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

from orchestune import github
from orchestune.dispatch_actor_verification import (
    _apply_actor_verification,
    _decide_actor_verification,
)
from orchestune.dispatch_config import DispatcherConfig
from orchestune.dispatch_escalation import _rule_changes_requested
from orchestune.dispatch_gc import (
    _collect_zombies_and_timeouts,
    _rule_completed,
    _rule_not_needed,
    _rule_stale_entry,
)
from orchestune.dispatch_launch import (
    _apply_duplicate_skip,
    _decide_duplicate_candidates,
    _get_stack_eligible_tasks,
    _launch_selected_tasks,
)
from orchestune.dispatch_locks import (
    ExternalLockScanResult,
    _strip_remote_prefix,
    scan_external_locks,
)
from orchestune.dispatch_rebase import _rule_auto_rebase, _rule_footprint_deviation
from orchestune.dispatch_recovery import _extract_raw_subtask_id, recover_run_state
from orchestune.dispatch_rules import CycleContext, RuleChain, _ActiveWorktreeAggregates
from orchestune.dispatch_scoring import (
    Task,
    parse_task_from_issue,
    quota_available,
    select_next_tasks,
)
from orchestune.dispatch_state import (
    ActiveWorktree,
    RunState,
    load_run_state,
    save_run_state,
)
from orchestune.dispatch_worktree import file_lock
from orchestune.github import IssueRecord, PrRecord


@dataclass
class CycleReport:
    selected: list[Task]
    quota_slots_available: int
    lock_changes: dict[str, list[Task]]
    deviation_events: list[dict]
    completion_events: list[dict]
    promotion_events: list[dict]
    applied: bool


def build_event_log_entry(report: CycleReport, now: float) -> dict:
    """#239: KPI A1〜A4/C2/C3集計用に、1サイクル分のイベントをJSON Lines化する。"""
    return {
        "timestamp": now,
        "quota_slots_available": report.quota_slots_available,
        "selected": [
            {"issue_number": t.issue_number, "subtask_id": t.subtask_id}
            for t in report.selected
        ],
        "deviation_events": report.deviation_events,
        "completion_events": report.completion_events,
        "promotion_events": report.promotion_events,
    }


def append_event_log(entry: dict, path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


# active worktreeごとの判定は、それぞれ対応するact側モジュールに定義された
# ruleとして実装されている(#86, dispatch_cycle.pyは条件判定そのものを持たない)。
# ここでは、それらをどの優先順位で評価するか(early/mainの2つのRuleChain)の
# 組み立てのみを行う。
#
# - status:not-needed / staleな帳簿エントリの検知は、他のどの判定よりも
#   先に評価する必要があるため「早期チェーン」として分離している
#   (該当すればそのactive worktreeへの以後の判定はすべてスキップする)。
# - 完了検知・CHANGES_REQUESTEDエスカレーション・自動リベース・footprint逸脱
#   検知は「主チェーン」として、この優先順位で評価する。
_EARLY_ACTIVE_WORKTREE_RULES = RuleChain(
    rules=[
        _rule_not_needed,
        _rule_stale_entry,
    ]
)

_MAIN_ACTIVE_WORKTREE_RULES = RuleChain(
    rules=[
        _rule_completed,
        _rule_changes_requested,
        _rule_auto_rebase,
        _rule_footprint_deviation,
    ]
)


def _process_active_worktrees(
    ctx: CycleContext,
) -> tuple[list[dict], list[dict], bool, set[str]]:
    """#192/#193/#200: active worktreeごとの完了検知・footprint逸脱処理。

    完了と判定したエントリは（apply時）run_state.active_worktreesから
    除去してクオータを解放し、以後のfootprint逸脱チェックはスキップする。

    新しい判断パターンを追加する場合、このループ自体は変更せず、対応する
    ruleを対応するact側モジュールに書いて、`_EARLY_ACTIVE_WORKTREE_RULES`/
    `_MAIN_ACTIVE_WORKTREE_RULES`に追加するだけでよい（#86）。
    """
    aggregates = _ActiveWorktreeAggregates()

    for key, active in list(ctx.run_state.active_worktrees.items()):
        active_task = ctx.tasks_by_issue.get(active.issue_number)

        if _EARLY_ACTIVE_WORKTREE_RULES.run(ctx, key, active, active_task, aggregates):
            continue

        if active.forced_serial:
            aggregates.any_forced_serial = True

        # _MAIN_ACTIVE_WORKTREE_RULESの末尾(_rule_footprint_deviation)は必ず
        # 非Noneかつterminalな結果を返すため、戻り値を見る必要はない。
        _MAIN_ACTIVE_WORKTREE_RULES.run(ctx, key, active, active_task, aggregates)

    return (
        aggregates.completion_events,
        aggregates.deviation_events,
        aggregates.any_forced_serial,
        aggregates.completed_subtask_ids,
    )


def _candidate_conflicts_with_forced_serial_active(
    candidate: Task,
    active: ActiveWorktree,
    active_task: Task | None,
) -> bool:
    active_footprint = active.declared_footprint
    active_subtask_id = ""
    active_depends_on: tuple[str, ...] = ()
    if active_task is not None:
        active_footprint = active_task.footprint or active.declared_footprint
        active_subtask_id = active_task.subtask_id
        active_depends_on = active_task.depends_on

    if set(candidate.footprint) & set(active_footprint):
        return True

    if active_subtask_id and active_subtask_id in candidate.depends_on:
        return True

    if candidate.subtask_id and candidate.subtask_id in active_depends_on:
        return True

    return False


def _filter_candidates_for_forced_serial(
    candidate_tasks: list[Task],
    run_state: RunState,
    tasks_by_issue: dict[int, Task],
) -> list[Task]:
    forced_serial_actives = [
        (active, tasks_by_issue.get(active.issue_number))
        for active in run_state.active_worktrees.values()
        if active.forced_serial
    ]
    if not forced_serial_actives:
        return candidate_tasks

    return [
        candidate
        for candidate in candidate_tasks
        if not any(
            _candidate_conflicts_with_forced_serial_active(
                candidate, active, active_task
            )
            for active, active_task in forced_serial_actives
        )
    ]


def _decide_blocked_promotions(
    blocked_issues: list[IssueRecord],
    done_issues: list[IssueRecord],
    completed_subtask_ids: set[str],
    tasks_by_issue: dict[int, Task],
) -> list[Task]:
    """#193: 依存先が全て解決したstatus:blockedタスクを副作用なしで判定する。

    #280: `done_issues`には`status:done`と`status:not-needed`の両方を
    呼び出し側で合流させて渡すことで、対応不要と判定された依存先も
    「解決済み」として扱われる（このタスク自体は依存先の状態を区別しない）。
    """
    done_subtask_ids = {
        tasks_by_issue[issue.number].subtask_id
        for issue in done_issues
        if issue.number in tasks_by_issue and tasks_by_issue[issue.number].subtask_id
    } | completed_subtask_ids

    promotable = []
    for issue in blocked_issues:
        task = tasks_by_issue.get(issue.number)
        if task is None or not task.depends_on:
            continue
        if not all(dep in done_subtask_ids for dep in task.depends_on):
            continue
        promotable.append(task)
    return promotable


def _apply_blocked_promotions(
    promotable: list[Task], config: DispatcherConfig
) -> list[dict]:
    events: list[dict] = []
    for task in promotable:
        if config.apply:
            github.remove_label(task.issue_number, "status:blocked")
            github.add_label(task.issue_number, "status:queued")
        events.append(
            {"issue_number": task.issue_number, "subtask_id": task.subtask_id}
        )
    return events


def _promote_blocked_tasks(
    blocked_issues: list[IssueRecord],
    done_issues: list[IssueRecord],
    completed_subtask_ids: set[str],
    tasks_by_issue: dict[int, Task],
    config: DispatcherConfig,
) -> list[dict]:
    """decide+applyの薄いラッパー（呼び出し互換のため維持）。"""
    promotable = _decide_blocked_promotions(
        blocked_issues, done_issues, completed_subtask_ids, tasks_by_issue
    )
    return _apply_blocked_promotions(promotable, config)


def _decide_external_lock_sync(
    tasks_by_issue: dict[int, Task],
    prs: list[PrRecord],
    run_state: RunState,
) -> ExternalLockScanResult:
    """githubからの読み取り(list_remote_branches/branch_changed_files)と
    scan_external_locksの純粋計算のみを行い、ラベルの書き込みは行わない。"""
    remote_branch_names = github.list_remote_branches()
    active_branches = [aw.branch for aw in run_state.active_worktrees.values()]
    pr_head_refs = {pr.head_ref for pr in prs}
    bare_branches = [
        b
        for b in remote_branch_names
        if _strip_remote_prefix(b) not in pr_head_refs
        and _strip_remote_prefix(b) not in active_branches
    ]
    remote_branch_footprints = [
        (
            _strip_remote_prefix(branch),
            tuple(github.branch_changed_files(branch)),
        )
        for branch in bare_branches
    ]

    all_tasks = list(tasks_by_issue.values())
    return scan_external_locks(
        all_tasks, remote_branch_footprints, prs, active_branches
    )


def _apply_external_lock_sync(
    lock_result: ExternalLockScanResult, config: DispatcherConfig
) -> None:
    if not config.apply:
        return
    for task in lock_result.to_lock:
        github.add_label(task.issue_number, "status:external-lock")
    for task in lock_result.to_unlock:
        github.remove_label(task.issue_number, "status:external-lock")
        if "status:done" not in task.status_labels:
            github.add_label(task.issue_number, "status:queued")


def _sync_external_locks(
    tasks_by_issue: dict[int, Task],
    prs: list[PrRecord],
    run_state: RunState,
    config: DispatcherConfig,
) -> ExternalLockScanResult:
    """decide+applyの薄いラッパー（呼び出し互換のため維持）。"""
    lock_result = _decide_external_lock_sync(tasks_by_issue, prs, run_state)
    _apply_external_lock_sync(lock_result, config)
    return lock_result


def _filter_by_parent(
    issues: list[IssueRecord], parent_issue_number: int | None
) -> list[IssueRecord]:
    """`parent_issue_number`が指定されている場合、親Issueが一致するものだけに絞る。"""
    if parent_issue_number is None:
        return issues
    return [
        i for i in issues if i.parent and i.parent.get("number") == parent_issue_number
    ]


def run_dispatch_cycle(config: DispatcherConfig) -> CycleReport:  # noqa: C901
    lock_path = Path(config.run_state_path).with_suffix(".lock")
    with file_lock(lock_path):
        run_state = load_run_state(config.run_state_path)
        now = time.time()

        if config.parent_issue_number is not None and config.apply:
            github.ensure_parent_branch(config.parent_issue_number)

        queued_issues = github.list_issues_by_label("status:queued")
        locked_issues = github.list_issues_by_label("status:external-lock")
        in_progress_issues = github.list_issues_by_label("status:in-progress")
        blocked_issues = github.list_issues_by_label("status:blocked")
        # #236: 完了Issueは人間が通常のGitHub運用でCloseすることが多いため、
        # 依存解決判定はclosedなIssueも含めて検索する。
        done_issues = github.list_issues_by_label("status:done", state="all")
        # #280: セッションがstatus:not-neededを付与すると同時にstatus:in-progressを
        # 外すため、in_progress_issuesの一覧には現れなくなる。tasks_by_issueに含めて
        # おかないと_process_active_worktrees側で完了検知できず、依存解決からも
        # 漏れてしまう（closedなIssueもクローズ後の依存解決に必要なためstate="all"）。
        not_needed_issues = github.list_issues_by_label(
            "status:not-needed", state="all"
        )

        # 自己修復（ステート復元・不整合修復）
        # run_state.json が存在しない場合、かつ apply=True の場合のみ復元処理を実行する
        if config.apply and not Path(config.run_state_path).exists():
            if recover_run_state(run_state, in_progress_issues, config):
                save_run_state(run_state, config.run_state_path)

        # parent_issue_number が指定されている場合、親Issueが一致する子Issueのみにフィルタリングする
        queued_issues = _filter_by_parent(queued_issues, config.parent_issue_number)
        locked_issues = _filter_by_parent(locked_issues, config.parent_issue_number)
        in_progress_issues = _filter_by_parent(
            in_progress_issues, config.parent_issue_number
        )
        blocked_issues = _filter_by_parent(blocked_issues, config.parent_issue_number)
        done_issues = _filter_by_parent(done_issues, config.parent_issue_number)
        not_needed_issues = _filter_by_parent(
            not_needed_issues, config.parent_issue_number
        )

        all_issues = [
            *queued_issues,
            *locked_issues,
            *in_progress_issues,
            *blocked_issues,
            *done_issues,
            *not_needed_issues,
        ]

        issue_to_subtask_id = {}
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

        prs = github.list_open_prs()

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

        ctx = CycleContext(
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
        )

        (
            completion_events,
            deviation_events,
            any_forced_serial,
            completed_subtask_ids,
        ) = _process_active_worktrees(ctx)

        gc_events = _collect_zombies_and_timeouts(
            run_state,
            tasks_by_issue,
            config,
        )
        completion_events.extend(gc_events)

        promotion_events = _promote_blocked_tasks(
            blocked_issues,
            done_issues + not_needed_issues,
            completed_subtask_ids,
            tasks_by_issue,
            config,
        )

        lock_result = _sync_external_locks(tasks_by_issue, prs, run_state, config)

        newly_locked = {t.issue_number for t in lock_result.to_lock}
        queued_candidates = [
            tasks_by_issue[issue.number]
            for issue in queued_issues
            if issue.number not in newly_locked
        ]

        # #119: status:queuedラベルを付与したactorのリポジトリ権限を検証し、
        # 権限不足のタスクを起動候補から除外する（status:blockedからのスタッキング
        # 起動であるstack_eligible_tasksは対象外）。
        actor_decisions = _decide_actor_verification(queued_candidates)
        queued_candidates = _apply_actor_verification(actor_decisions, config)

        stack_eligible_tasks, task_to_base_branch = _get_stack_eligible_tasks(
            blocked_issues,
            tasks_by_issue,
            done_subtask_ids,
            ci_passed_pr_subtask_ids,
            subtask_branch_map,
            completed_subtask_ids=completed_subtask_ids,
        )

        candidate_tasks = queued_candidates + stack_eligible_tasks

        # 重複起動の防止: 既にオープンなPRが存在するcandidate_tasksを検知し、
        # 起動対象から除外して status:blocked-human-review へ移行させる。
        duplicate_decisions = _decide_duplicate_candidates(candidate_tasks, ctx)
        candidate_tasks = _apply_duplicate_skip(duplicate_decisions, ctx)

        if any_forced_serial:
            candidate_tasks = _filter_candidates_for_forced_serial(
                candidate_tasks,
                run_state,
                tasks_by_issue,
            )

        quota_slots = quota_available(
            run_state,
            now,
            config.max_concurrent,
            config.max_launches_per_window,
            config.window_seconds,
        )
        selected = select_next_tasks(
            candidate_tasks,
            run_state,
            now,
            config.max_concurrent,
            config.max_launches_per_window,
            config.window_seconds,
        )

        if config.apply:
            selected = _launch_selected_tasks(
                selected,
                task_to_base_branch,
                candidate_tasks,
                run_state,
                now,
                config,
            )
            run_state.last_reconciled_at = now
            save_run_state(run_state, config.run_state_path)

        report = CycleReport(
            selected=selected,
            quota_slots_available=quota_slots,
            lock_changes={
                "to_lock": lock_result.to_lock,
                "to_unlock": lock_result.to_unlock,
            },
            deviation_events=deviation_events,
            completion_events=completion_events,
            promotion_events=promotion_events,
            applied=config.apply,
        )

        if config.apply:
            append_event_log(build_event_log_entry(report, now), config.events_log_path)

        return report
