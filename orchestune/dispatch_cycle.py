"""1サイクル分のディスパッチオーケストレーション本体。"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

from orchestune import github
from orchestune.dispatch_gc import (
    _collect_zombies_and_timeouts,
    _finalize_completed_worktree,
    _finalize_not_needed_worktree,
    is_process_alive,
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
    check_footprint_deviation,
    scan_external_locks,
)
from orchestune.dispatch_rebase import _handle_footprint_deviation, _try_auto_rebase
from orchestune.dispatch_recovery import _extract_raw_subtask_id, recover_run_state
from orchestune.dispatch_scoring import (
    Task,
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
    DispatchHandle,
    DispatchTarget,
    LocalProcessDispatchTarget,
)
from orchestune.dispatch_worktree import file_lock
from orchestune.github import IssueRecord, PrRecord


@dataclass
class DispatcherConfig:
    max_concurrent: int = 2
    max_launches_per_window: int = 1
    window_seconds: int = 3600
    run_state_path: Path = Path("run_state.json")
    worktree_root: Path = Path("worktrees")
    log_dir: Path = Path("logs")
    events_log_path: Path = Path("events.jsonl")
    parent_issue_number: int | None = None
    apply: bool = False
    dispatch_target: DispatchTarget | None = None
    deviation_buffer_lines: int = 5
    max_recompute_retries: int = 2
    task_timeout_seconds: int = 0
    # #282: status:not-needed判定の独立検証レビュー（保留分）の永続化先。
    not_needed_review_state_path: Path = Path("not_needed_review_state.json")

    def __post_init__(self) -> None:
        if self.dispatch_target is None:
            self.dispatch_target = LocalProcessDispatchTarget(log_dir=self.log_dir)


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


def _decide_stale_active_entry(
    active: ActiveWorktree, active_task: Task | None
) -> dict | None:
    """githubラベルを正として、run_state側に残った古い帳簿エントリ（stale)かを
    副作用なしで判定する。staleであればイベントdictを返す。"""
    if (
        active_task is not None
        and "status:in-progress" not in active_task.status_labels
    ):
        # run_stateへの登録(save_run_state)は起動成功直後に、GitHubラベルの
        # status:in-progress付与はその後に行う順序になっているため、この間で
        # クラッシュした場合（あるいは完了/エスカレーション処理でラベルだけ
        # 先に更新されてクラッシュした場合）、GitHub側のラベルは
        # status:in-progressでなくなっているのにrun_state側にだけ古い
        # エントリが残ることがある。GitHubラベルを正として、この古い帳簿
        # エントリを破棄する（ゾンビGCの拡張）。
        return {
            "issue_number": active.issue_number,
            "subtask_id": active_task.subtask_id,
            "action": "stale_active_entry_discarded",
            "reason": (
                "issue label is no longer status:in-progress "
                f"(labels={sorted(active_task.status_labels)})"
            ),
        }
    return None


def _apply_stale_active_entry_discard(
    run_state: RunState, key: str, config: DispatcherConfig
) -> None:
    if config.apply:
        del run_state.active_worktrees[key]


def _decide_changes_requested_escalation(
    active_task: Task | None, changes_requested_subtask_ids: set[str]
) -> bool:
    """依存元PRがCHANGES_REQUESTEDを受けているかを副作用なしで判定する。"""
    if active_task and active_task.depends_on:
        return any(
            dep in changes_requested_subtask_ids for dep in active_task.depends_on
        )
    return False


def _apply_changes_requested_escalation(
    active: ActiveWorktree,
    active_task: Task,
    key: str,
    run_state: RunState,
    config: DispatcherConfig,
) -> dict:
    """依存元PRがCHANGES_REQUESTEDになったタスクを一時停止する
    （プロセスkill・githubラベル/コメント・run_state削除はすべてact）。"""
    if config.apply:
        if active.pid:
            try:
                os.kill(active.pid, 9)
            except OSError:
                pass
        github.remove_label(active.issue_number, "status:in-progress")
        github.add_label(active.issue_number, "status:blocked-human-review")
        github.add_comment(
            active.issue_number,
            "依存元PRが変更要求（Request Changes）を受けたため、スタックされたタスクを一時停止しました。",
        )
        del run_state.active_worktrees[key]
    return {
        "issue_number": active.issue_number,
        "subtask_id": active_task.subtask_id,
        "action": "escalated_due_to_changes_requested",
    }


def _process_active_worktrees(  # noqa: C901
    run_state: RunState,
    tasks_by_issue: dict[int, Task],
    issue_number_by_subtask_id: dict[str, int],
    ci_passed_pr_subtask_ids: set[str],
    changes_requested_subtask_ids: set[str],
    subtask_branch_map: dict[str, str],
    config: DispatcherConfig,
) -> tuple[list[dict], list[dict], bool, set[str]]:
    """#192/#193/#200: active worktreeごとの完了検知・footprint逸脱処理。

    完了と判定したエントリは（apply時）run_state.active_worktreesから
    除去してクオータを解放し、以後のfootprint逸脱チェックはスキップする。

    各分岐の判定(decide)と実処理(act)は、本関数または委譲先の
    dispatch_gc/dispatch_rebaseモジュール内でそれぞれ分離されている。
    """
    completion_events: list[dict] = []
    deviation_events: list[dict] = []
    any_forced_serial = False
    completed_subtask_ids: set[str] = set()

    for key, active in list(run_state.active_worktrees.items()):
        active_task = tasks_by_issue.get(active.issue_number)

        if active_task is not None and "status:not-needed" in active_task.status_labels:
            # #280: セッションが「対応不要」と判断した場合、コミット・PRを作らない
            # ためclosingIssuesReferences等の完了シグナルが発生せず、
            # _is_worktree_complete()（PID/PR存在ベース）は永遠にFalseを返し続ける。
            # ラベル検知を最優先の完了シグナルとして扱い、下のstale判定より先に処理する。
            completion_event = _finalize_not_needed_worktree(
                active, active_task, config
            )
            completion_events.append(completion_event)
            # #282: 即時クローズ・検証レビューへの委譲のどちらの経路でも、
            # 対応不要の根拠自体は「mainに既に実装されている」ことなので、
            # Issueクローズの可否とは独立に依存関係は解決済みとして扱ってよい。
            if completion_event["action"] in (
                "not_needed",
                "not_needed_review_dispatched",
            ):
                if active_task.subtask_id:
                    completed_subtask_ids.add(active_task.subtask_id)
                if config.apply:
                    del run_state.active_worktrees[key]
            continue

        stale_event = _decide_stale_active_entry(active, active_task)
        if stale_event is not None:
            completion_events.append(stale_event)
            _apply_stale_active_entry_discard(run_state, key, config)
            continue

        if active.forced_serial:
            any_forced_serial = True

        if _is_worktree_complete(active, config):
            completion_event = _finalize_completed_worktree(active, active_task, config)
            completion_events.append(completion_event)
            if completion_event["action"] == "completed":
                if active_task is not None and active_task.subtask_id:
                    completed_subtask_ids.add(active_task.subtask_id)
                if config.apply:
                    run_state.completed_worktrees.append(
                        CompletedWorktree(
                            issue_number=active.issue_number,
                            subtask_id=active_task.subtask_id if active_task else "",
                            branch=active.branch,
                            started_at=active.started_at,
                            completed_at=time.time(),
                            recompute_count=active.recompute_count,
                            forced_serial=active.forced_serial,
                            commit_sha=completion_event.get("commit_sha"),
                        )
                    )
                    del run_state.active_worktrees[key]
                continue
            if completion_event["action"] == "completed_no_commits":
                # #74: 実コミットの無い完了は依存解決の対象にしない
                # (completed_subtask_idsに加えない)が、worktree・ラベルは
                # dispatch_gc側で既に片付け済みのため、クオータ解放のために
                # run_state側のエントリは削除する。
                if config.apply:
                    del run_state.active_worktrees[key]
                continue

        # 自動リベースや逸脱判定の前に、CHANGES_REQUESTED になった親を持つかチェックする (#185)
        if _decide_changes_requested_escalation(
            active_task, changes_requested_subtask_ids
        ):
            assert active_task is not None
            completion_events.append(
                _apply_changes_requested_escalation(
                    active, active_task, key, run_state, config
                )
            )
            continue

        # 自動リベース判定＆実行 (#201)
        process_alive = is_process_alive(active.pid)
        if process_alive and _try_auto_rebase(
            active,
            active_task,
            key,
            run_state,
            ci_passed_pr_subtask_ids,
            subtask_branch_map,
            config,
        ):
            continue

        deviated = check_footprint_deviation(
            active.worktree_path,
            active.declared_footprint,
            base=active.base_branch,
            min_changed_lines=config.deviation_buffer_lines,
        )
        if not deviated:
            continue

        event = _handle_footprint_deviation(
            active, deviated, tasks_by_issue, issue_number_by_subtask_id, config
        )
        if event["action"] in ("forced_serial", "already_forced_serial"):
            any_forced_serial = True
        deviation_events.append(event)

    return completion_events, deviation_events, any_forced_serial, completed_subtask_ids


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


def _is_worktree_complete(active: ActiveWorktree, config: DispatcherConfig) -> bool:
    """#215: `external_id`が設定されている（ローカルpid以外でディスパッチされた）
    active worktreeは、設定されたdispatch_targetの`is_complete`に完了判定を委譲する。
    それ以外（従来通りのローカルsubprocess起動）は`is_process_alive`ベースのまま。"""
    if active.external_id is not None:
        handle = DispatchHandle(
            pid=active.pid,
            external_id=active.external_id,
            external_url=active.external_url,
            branch_name=active.branch,
            issue_number=active.issue_number,
        )
        assert config.dispatch_target is not None
        return config.dispatch_target.is_complete(handle)
    return not is_process_alive(active.pid)


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
        if config.parent_issue_number is not None:
            queued_issues = [
                i
                for i in queued_issues
                if i.parent and i.parent.get("number") == config.parent_issue_number
            ]
            locked_issues = [
                i
                for i in locked_issues
                if i.parent and i.parent.get("number") == config.parent_issue_number
            ]
            in_progress_issues = [
                i
                for i in in_progress_issues
                if i.parent and i.parent.get("number") == config.parent_issue_number
            ]
            blocked_issues = [
                i
                for i in blocked_issues
                if i.parent and i.parent.get("number") == config.parent_issue_number
            ]
            done_issues = [
                i
                for i in done_issues
                if i.parent and i.parent.get("number") == config.parent_issue_number
            ]
            not_needed_issues = [
                i
                for i in not_needed_issues
                if i.parent and i.parent.get("number") == config.parent_issue_number
            ]

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

        (
            completion_events,
            deviation_events,
            any_forced_serial,
            completed_subtask_ids,
        ) = _process_active_worktrees(
            run_state,
            tasks_by_issue,
            issue_number_by_subtask_id,
            ci_passed_pr_subtask_ids,
            changes_requested_subtask_ids,
            subtask_branch_map,
            config,
        )

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

        stack_eligible_tasks, task_to_base_branch = _get_stack_eligible_tasks(
            blocked_issues,
            tasks_by_issue,
            done_subtask_ids,
            ci_passed_pr_subtask_ids,
            subtask_branch_map,
        )

        candidate_tasks = queued_candidates + stack_eligible_tasks

        # 重複起動の防止: 既にオープンなPRが存在するcandidate_tasksを検知し、
        # 起動対象から除外して status:blocked-human-review へ移行させる。
        duplicate_decisions = _decide_duplicate_candidates(
            candidate_tasks, pr_by_branch, prs, run_state
        )
        candidate_tasks = _apply_duplicate_skip(duplicate_decisions, config)

        if any_forced_serial:
            quota_slots = 0
            selected: list[Task] = []
        else:
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
