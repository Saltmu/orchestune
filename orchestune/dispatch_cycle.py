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


def _group_by_status(issues: list[IssueRecord]) -> IssuesByStatus:
    """#156: `github.list_sub_issues`が返す親Issue配下の全Issueを、
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
    `github.list_sub_issues`による親Issue起点のfast pathを使う。
    """
    if config.parent_issue_number is not None:
        return _group_by_status(github.list_sub_issues(config.parent_issue_number))

    return IssuesByStatus(
        queued=github.list_issues_by_label("status:queued"),
        locked=github.list_issues_by_label("status:external-lock"),
        in_progress=github.list_issues_by_label("status:in-progress"),
        blocked=github.list_issues_by_label("status:blocked"),
        # #236: 完了Issueは人間が通常のGitHub運用でCloseすることが多いため、
        # 依存解決判定はclosedなIssueも含めて検索する。
        done=github.list_issues_by_label("status:done", state="all"),
        # #280: セッションがstatus:not-neededを付与すると同時にstatus:in-progressを
        # 外すため、in_progress側の一覧には現れなくなる。tasks_by_issueに含めて
        # おかないと_process_active_worktrees側で完了検知できず、依存解決からも
        # 漏れてしまう（closedなIssueもクローズ後の依存解決に必要なためstate="all"）。
        not_needed=github.list_issues_by_label("status:not-needed", state="all"),
    )


def _self_heal_run_state(
    run_state: RunState,
    config: DispatcherConfig,
) -> None:
    """自己修復（ステート復元・不整合修復）。

    run_state.json が存在しない場合、かつ apply=True の場合のみ復元処理を実行する。

    #156: `run_state.json`は複数の親Issue（big rock）にまたがって共有されうる
    ため、`parent_issue_number`指定時のfast pathでスコープが絞られた
    `IssuesByStatus`は使わず、常にリポジトリ全体のstatus:in-progress Issueを
    読み直す。範囲を絞ってしまうと、他の親Issue配下のactive worktreeが
    復元されないまま`run_state.json`が新規保存され、以後永遠に復元機会を
    失うおそれがある。
    """
    if not (config.apply and not Path(config.run_state_path).exists()):
        return
    in_progress_issues = github.list_issues_by_label("status:in-progress")
    if recover_run_state(run_state, in_progress_issues, config):
        save_run_state(run_state, config.run_state_path)


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
    )


def _determine_candidate_tasks(
    ctx: CycleContext,
    issues: IssuesByStatus,
    lock_result: ExternalLockScanResult,
    completed_subtask_ids: set[str],
    any_forced_serial: bool,
) -> tuple[list[Task], dict[int, str]]:
    """起動候補タスクを、外部ロック・actor権限・スタッキング可否・重複起動・
    強制直列化の各観点で絞り込んで確定させる。"""
    newly_locked = {t.issue_number for t in lock_result.to_lock}
    queued_candidates = [
        ctx.tasks_by_issue[issue.number]
        for issue in issues.queued
        if issue.number not in newly_locked
    ]

    # #119: status:queuedラベルを付与したactorのリポジトリ権限を検証し、
    # 権限不足のタスクを起動候補から除外する（status:blockedからのスタッキング
    # 起動であるstack_eligible_tasksは対象外）。
    actor_decisions = _decide_actor_verification(queued_candidates)
    queued_candidates = _apply_actor_verification(actor_decisions, ctx.config)

    stack_eligible_tasks, task_to_base_branch = _get_stack_eligible_tasks(
        issues.blocked,
        ctx.tasks_by_issue,
        ctx.done_subtask_ids,
        ctx.ci_passed_pr_subtask_ids,
        ctx.subtask_branch_map,
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
            ctx.run_state,
            ctx.tasks_by_issue,
        )

    return candidate_tasks, task_to_base_branch


def _finalize_launch(
    selected: list[Task],
    task_to_base_branch: dict[int, str],
    candidate_tasks: list[Task],
    ctx: CycleContext,
    now: float,
    config: DispatcherConfig,
) -> list[Task]:
    """apply時のみ、選出タスクを実起動しrun_stateを永続化する。"""
    if not config.apply:
        return selected
    selected = _launch_selected_tasks(
        selected,
        task_to_base_branch,
        candidate_tasks,
        ctx.run_state,
        now,
        config,
    )
    ctx.run_state.last_reconciled_at = now
    save_run_state(ctx.run_state, config.run_state_path)
    return selected


def _handle_blocked_recompute_recovery(
    issues: IssuesByStatus,
    run_state: RunState,
    ctx: CycleContext,
    completed_subtask_ids: set[str],
    config: DispatcherConfig,
) -> list[dict]:
    """フットプリント逸脱によるブロック（status:blocked-recompute）の自動復帰（解除）処理を行う。"""
    recompute_resolved_promoted_events: list[dict] = []
    blocked_recompute_issues = [
        issue for issue in issues.all() if "status:blocked-recompute" in issue.labels
    ]

    if not blocked_recompute_issues:
        return recompute_resolved_promoted_events

    # 現在アクティブなワークツリーによる競合対象のサブタスクIDを収集
    active_conflict_subtask_ids = set()
    from orchestune.dispatch_rebase import _build_subtasks_for_recompute

    subtasks_for_recompute = _build_subtasks_for_recompute(ctx.tasks_by_issue)
    for active in run_state.active_worktrees.values():
        active_task = ctx.tasks_by_issue.get(active.issue_number)
        if not active_task or not active_task.subtask_id:
            continue

        from orchestune.dag import recompute_dag_for_footprint_change
        from orchestune.dispatch_locks import check_footprint_deviation

        deviated = check_footprint_deviation(
            active.worktree_path,
            active.declared_footprint,
            base=active.base_branch,
            min_changed_lines=config.deviation_buffer_lines,
        )
        merged_footprint = tuple(dict.fromkeys([*active.declared_footprint, *deviated]))
        try:
            _, conflicts = recompute_dag_for_footprint_change(
                subtasks_for_recompute,
                active_task.subtask_id,
                updated_footprint=merged_footprint,
            )
            for conflict in conflicts:
                if conflict.blocked_subtask_id:
                    active_conflict_subtask_ids.add(conflict.blocked_subtask_id)
        except Exception:
            pass

    for issue in blocked_recompute_issues:
        task = ctx.tasks_by_issue.get(issue.number)
        if not task or not task.subtask_id:
            continue

        if task.subtask_id not in active_conflict_subtask_ids:
            if config.apply:
                github.remove_label(issue.number, "status:blocked-recompute")

            done_subtask_ids = ctx.done_subtask_ids | completed_subtask_ids
            has_pending_deps = any(
                dep not in done_subtask_ids for dep in task.depends_on
            )

            if not has_pending_deps:
                if config.apply:
                    github.remove_label(issue.number, "status:blocked")
                    github.add_label(issue.number, "status:queued")
                recompute_resolved_promoted_events.append(
                    {"issue_number": issue.number, "subtask_id": task.subtask_id}
                )

    return recompute_resolved_promoted_events


def run_dispatch_cycle(config: DispatcherConfig) -> CycleReport:
    lock_path = Path(config.run_state_path).with_suffix(".lock")
    with file_lock(lock_path):
        run_state = load_run_state(config.run_state_path)
        now = time.time()

        if config.parent_issue_number is not None and config.apply:
            github.ensure_parent_branch(config.parent_issue_number)

        issues = _fetch_issues(config)
        _self_heal_run_state(run_state, config)
        issues = issues.filtered_by_parent(config.parent_issue_number)

        ctx = _build_cycle_context(issues, run_state, config)

        (
            completion_events,
            deviation_events,
            any_forced_serial,
            completed_subtask_ids,
        ) = _process_active_worktrees(ctx)

        gc_events = _collect_zombies_and_timeouts(
            ctx.run_state, ctx.tasks_by_issue, config
        )
        completion_events.extend(gc_events)

        promotion_events = _promote_blocked_tasks(
            issues.blocked,
            issues.done + issues.not_needed,
            completed_subtask_ids,
            ctx.tasks_by_issue,
            config,
        )

        # 決定論的な自動復帰（ブロック解除）処理
        recompute_resolved_promoted_events = _handle_blocked_recompute_recovery(
            issues, run_state, ctx, completed_subtask_ids, config
        )
        promotion_events.extend(recompute_resolved_promoted_events)

        lock_result = _sync_external_locks(
            ctx.tasks_by_issue, ctx.prs, ctx.run_state, config
        )

        candidate_tasks, task_to_base_branch = _determine_candidate_tasks(
            ctx, issues, lock_result, completed_subtask_ids, any_forced_serial
        )

        # 同一サイクルでfootprint逸脱により新たにブロックされたタスクを除外する
        newly_blocked_recompute_issues = set()
        for event in deviation_events:
            if event.get("action") == "recomputed":
                for conflict in event.get("conflicts", []):
                    blocked_id = conflict.get("blocked_subtask_id")
                    if blocked_id:
                        num = ctx.issue_number_by_subtask_id.get(blocked_id)
                        if num is not None:
                            newly_blocked_recompute_issues.add(num)

        if newly_blocked_recompute_issues:
            candidate_tasks = [
                t
                for t in candidate_tasks
                if t.issue_number not in newly_blocked_recompute_issues
            ]

        quota_slots = quota_available(
            ctx.run_state,
            now,
            config.max_concurrent,
            config.max_launches_per_window,
            config.window_seconds,
        )
        selected = select_next_tasks(
            candidate_tasks,
            ctx.run_state,
            now,
            config.max_concurrent,
            config.max_launches_per_window,
            config.window_seconds,
        )
        selected = _finalize_launch(
            selected, task_to_base_branch, candidate_tasks, ctx, now, config
        )

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
