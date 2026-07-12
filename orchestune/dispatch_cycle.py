"""1サイクル分のディスパッチオーケストレーション本体。"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from orchestune import github
from orchestune.dispatch_actor_verification import (
    _apply_actor_verification,
    _decide_actor_verification,
)
from orchestune.dispatch_escalation import apply_human_review_escalation
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


@dataclass
class CycleContext:
    """1サイクル分の読み取り専用データをまとめたコンテキスト。

    decide/act関数の引数を位置引数の羅列にせず、新しい判断パターンが追加の
    データを必要とする場合の引数伝播を、このコンテキストへの1フィールド追加に
    閉じ込めることを目的とする（#86）。
    """

    run_state: RunState
    tasks_by_issue: dict[int, Task]
    issue_number_by_subtask_id: dict[str, int]
    done_subtask_ids: set[str]
    ci_passed_pr_subtask_ids: set[str]
    changes_requested_subtask_ids: set[str]
    subtask_branch_map: dict[str, str]
    prs: list[PrRecord]
    pr_by_branch: dict[str, PrRecord]
    config: DispatcherConfig


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
        apply_human_review_escalation(
            active.issue_number,
            ("status:in-progress",),
            "依存元PRが変更要求（Request Changes）を受けたため、スタックされたタスクを一時停止しました。",
        )
        del run_state.active_worktrees[key]
    return {
        "issue_number": active.issue_number,
        "subtask_id": active_task.subtask_id,
        "action": "escalated_due_to_changes_requested",
    }


@dataclass
class ActiveWorktreeRuleOutcome:
    """1つの判定ルールがactive worktreeに対して下した結果。

    `terminal=True`の場合、このactive worktreeに対する以降のルール評価を
    打ち切り次のactive worktreeへ進む。`terminal=False`の場合は次のルールを
    引き続き試す（例: dirty worktreeのため完了判定を見送った場合でも、
    CHANGES_REQUESTEDや自動リベースのチェックは継続する必要がある）。
    """

    completion_event: dict | None = None
    deviation_event: dict | None = None
    completed_subtask_id: str | None = None
    forced_serial: bool = False
    terminal: bool = True


@dataclass
class _ActiveWorktreeAggregates:
    completion_events: list[dict] = field(default_factory=list)
    deviation_events: list[dict] = field(default_factory=list)
    any_forced_serial: bool = False
    completed_subtask_ids: set[str] = field(default_factory=set)


def _merge_active_worktree_outcome(
    aggregates: _ActiveWorktreeAggregates, outcome: ActiveWorktreeRuleOutcome
) -> None:
    if outcome.completion_event is not None:
        aggregates.completion_events.append(outcome.completion_event)
    if outcome.deviation_event is not None:
        aggregates.deviation_events.append(outcome.deviation_event)
    if outcome.completed_subtask_id is not None:
        aggregates.completed_subtask_ids.add(outcome.completed_subtask_id)
    if outcome.forced_serial:
        aggregates.any_forced_serial = True


def _rule_not_needed(
    ctx: CycleContext, key: str, active: ActiveWorktree, active_task: Task | None
) -> ActiveWorktreeRuleOutcome | None:
    """#280: status:not-neededラベル検知による即時完了処理。

    セッションが「対応不要」と判断した場合、コミット・PRを作らないため
    closingIssuesReferences等の完了シグナルが発生せず、`_rule_completed`
    （PID/PR存在ベース）は永遠にマッチしない。ラベル検知を最優先の完了
    シグナルとして扱い、stale判定より先に評価する。
    """
    if active_task is None or "status:not-needed" not in active_task.status_labels:
        return None

    completion_event = _finalize_not_needed_worktree(active, active_task, ctx.config)
    completed_subtask_id = None
    # #282: 即時クローズ・検証レビューへの委譲のどちらの経路でも、対応不要の
    # 根拠自体は「mainに既に実装されている」ことなので、Issueクローズの可否とは
    # 独立に依存関係は解決済みとして扱ってよい。
    if completion_event["action"] in ("not_needed", "not_needed_review_dispatched"):
        if active_task.subtask_id:
            completed_subtask_id = active_task.subtask_id
        if ctx.config.apply:
            del ctx.run_state.active_worktrees[key]

    return ActiveWorktreeRuleOutcome(
        completion_event=completion_event,
        completed_subtask_id=completed_subtask_id,
        terminal=True,
    )


def _rule_stale_entry(
    ctx: CycleContext, key: str, active: ActiveWorktree, active_task: Task | None
) -> ActiveWorktreeRuleOutcome | None:
    stale_event = _decide_stale_active_entry(active, active_task)
    if stale_event is None:
        return None
    _apply_stale_active_entry_discard(ctx.run_state, key, ctx.config)
    return ActiveWorktreeRuleOutcome(completion_event=stale_event, terminal=True)


def _rule_completed(
    ctx: CycleContext, key: str, active: ActiveWorktree, active_task: Task | None
) -> ActiveWorktreeRuleOutcome | None:
    if not _is_worktree_complete(active, ctx.config):
        return None

    completion_event = _finalize_completed_worktree(active, active_task, ctx.config)
    action = completion_event["action"]

    if action == "completed":
        completed_subtask_id = None
        if active_task is not None and active_task.subtask_id:
            completed_subtask_id = active_task.subtask_id
        if ctx.config.apply:
            ctx.run_state.completed_worktrees.append(
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
            del ctx.run_state.active_worktrees[key]
        return ActiveWorktreeRuleOutcome(
            completion_event=completion_event,
            completed_subtask_id=completed_subtask_id,
            terminal=True,
        )

    if action == "completed_no_commits":
        # #74: 実コミットの無い完了は依存解決の対象にしない
        # (completed_subtask_idsに加えない)が、worktree・ラベルは
        # dispatch_gc側で既に片付け済みのため、クオータ解放のために
        # run_state側のエントリは削除する。
        if ctx.config.apply:
            del ctx.run_state.active_worktrees[key]
        return ActiveWorktreeRuleOutcome(
            completion_event=completion_event, terminal=True
        )

    # action == "completion_skipped_dirty_worktree": イベントは記録するが、
    # このactive worktreeへの他の判定（CHANGES_REQUESTED/自動リベース/
    # footprint逸脱）は継続せずに人間が変更を確認するまで待つため、terminalにする。
    return ActiveWorktreeRuleOutcome(completion_event=completion_event, terminal=True)


def _rule_changes_requested(
    ctx: CycleContext, key: str, active: ActiveWorktree, active_task: Task | None
) -> ActiveWorktreeRuleOutcome | None:
    """#185: 自動リベースや逸脱判定の前に、CHANGES_REQUESTEDになった親を持つかチェックする。"""
    if not _decide_changes_requested_escalation(
        active_task, ctx.changes_requested_subtask_ids
    ):
        return None
    assert active_task is not None
    event = _apply_changes_requested_escalation(
        active, active_task, key, ctx.run_state, ctx.config
    )
    return ActiveWorktreeRuleOutcome(completion_event=event, terminal=True)


def _rule_auto_rebase(
    ctx: CycleContext, key: str, active: ActiveWorktree, active_task: Task | None
) -> ActiveWorktreeRuleOutcome | None:
    """#201: 自動リベース判定＆実行。"""
    if not is_process_alive(active.pid):
        return None
    if not _try_auto_rebase(
        active,
        active_task,
        key,
        ctx.run_state,
        ctx.done_subtask_ids,
        ctx.ci_passed_pr_subtask_ids,
        ctx.subtask_branch_map,
        ctx.config,
    ):
        return None
    return ActiveWorktreeRuleOutcome(terminal=True)


def _rule_footprint_deviation(
    ctx: CycleContext, key: str, active: ActiveWorktree, active_task: Task | None
) -> ActiveWorktreeRuleOutcome:
    """フォールバックルール: 他のどのルールにも該当しなかったactive worktreeに
    ついて、footprint逸脱の有無を判定する。ルールチェーンの末尾として、常に
    非Noneの結果を返し必ずこのactive worktreeの処理を終える。
    """
    deviated = check_footprint_deviation(
        active.worktree_path,
        active.declared_footprint,
        base=active.base_branch,
        min_changed_lines=ctx.config.deviation_buffer_lines,
    )
    if not deviated:
        return ActiveWorktreeRuleOutcome(terminal=True)

    event = _handle_footprint_deviation(
        active, deviated, ctx.tasks_by_issue, ctx.issue_number_by_subtask_id, ctx.config
    )
    forced_serial = event["action"] in ("forced_serial", "already_forced_serial")
    return ActiveWorktreeRuleOutcome(
        deviation_event=event, forced_serial=forced_serial, terminal=True
    )


_EARLY_ACTIVE_WORKTREE_RULES: list[
    Callable[
        [CycleContext, str, ActiveWorktree, Task | None],
        ActiveWorktreeRuleOutcome | None,
    ]
] = [
    _rule_not_needed,
    _rule_stale_entry,
]

_MAIN_ACTIVE_WORKTREE_RULES: list[
    Callable[
        [CycleContext, str, ActiveWorktree, Task | None],
        ActiveWorktreeRuleOutcome | None,
    ]
] = [
    _rule_completed,
    _rule_changes_requested,
    _rule_auto_rebase,
    _rule_footprint_deviation,
]


def _run_active_worktree_rules(
    rules: list[
        Callable[
            [CycleContext, str, ActiveWorktree, Task | None],
            ActiveWorktreeRuleOutcome | None,
        ]
    ],
    ctx: CycleContext,
    key: str,
    active: ActiveWorktree,
    active_task: Task | None,
    aggregates: _ActiveWorktreeAggregates,
) -> bool:
    """ruleを順に試し、非Noneの結果が返るたびaggregatesへ反映する。

    `terminal=True`の結果を得たら直ちにTrueを返して打ち切り、それ以外は
    次のruleを試し続ける。どのruleにも該当しなければFalseを返す。

    新しい判断パターンを追加する場合、このループ自体は変更せず、対応する
    `_rule_*`関数を書いて`_EARLY_ACTIVE_WORKTREE_RULES`/
    `_MAIN_ACTIVE_WORKTREE_RULES`に追加するだけでよい（#86）。
    """
    for rule in rules:
        outcome = rule(ctx, key, active, active_task)
        if outcome is None:
            continue
        _merge_active_worktree_outcome(aggregates, outcome)
        if outcome.terminal:
            return True
    return False


def _process_active_worktrees(
    ctx: CycleContext,
) -> tuple[list[dict], list[dict], bool, set[str]]:
    """#192/#193/#200: active worktreeごとの完了検知・footprint逸脱処理。

    完了と判定したエントリは（apply時）run_state.active_worktreesから
    除去してクオータを解放し、以後のfootprint逸脱チェックはスキップする。

    各分岐の判定(decide)と実処理(act)は、`_rule_*`関数または委譲先の
    dispatch_gc/dispatch_rebaseモジュール内でそれぞれ分離されている。
    """
    aggregates = _ActiveWorktreeAggregates()

    for key, active in list(ctx.run_state.active_worktrees.items()):
        active_task = ctx.tasks_by_issue.get(active.issue_number)

        if _run_active_worktree_rules(
            _EARLY_ACTIVE_WORKTREE_RULES, ctx, key, active, active_task, aggregates
        ):
            continue

        if active.forced_serial:
            aggregates.any_forced_serial = True

        # _MAIN_ACTIVE_WORKTREE_RULESの末尾(_rule_footprint_deviation)は必ず
        # 非Noneかつterminalな結果を返すため、戻り値を見る必要はない。
        _run_active_worktree_rules(
            _MAIN_ACTIVE_WORKTREE_RULES, ctx, key, active, active_task, aggregates
        )

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
