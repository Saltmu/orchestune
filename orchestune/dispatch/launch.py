"""起動候補の選定（スタック可否判定・重複起動防止）と、選出タスクの実起動。"""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Generic, TypeVar

from orchestune.branch_naming import build_task_branch_name
from orchestune.claim.contracts import (
    ClaimFailureReason,
    ClaimOutcome,
    ClaimRequest,
    ClaimStage,
    OwnerKind,
)
from orchestune.dispatch.cost_model import build_cost_model
from orchestune.dispatch.cycle_action_contracts import CycleQueries
from orchestune.dispatch.dependency_policy import (
    DependencyPolicyView,
    StackDecision,
    decide_stack_target,
)
from orchestune.dispatch.escalation import apply_human_review_escalation
from orchestune.dispatch.execution_profiles import (
    ExecutionSelection,
    resolve_task_execution_selection,
)
from orchestune.dispatch.labels import transition_status_label
from orchestune.dispatch.launch_attempts import (
    LaunchOutcomeUnknown,
    prepare_journaled_target,
)
from orchestune.dispatch.state import (
    ActiveWorktree,
    CompletedWorktree,
    RunState,
    load_run_state,
    save_run_state,
)
from orchestune.dispatch.worktree import LaunchResult, _launch_on_prepared_worktree
from orchestune.infra.git_cli import run_git
from orchestune.issue_parsing import (
    backfill_launch_history,
    launch_history_from_body,
    launch_history_in_window,
)
from orchestune.labels import StatusLabel
from orchestune.models import PrRecord
from orchestune.task_branch_resolution import BranchCapability
from orchestune.task_metadata import TaskMetadata

if TYPE_CHECKING:
    from orchestune.dispatch.config import DispatcherConfig
    from orchestune.dispatch.targets import DispatchTarget


LaunchCommitted = Callable[[ActiveWorktree], None]
TTask = TypeVar("TTask", bound=TaskMetadata)

# #943: `orchestune.claim.service`はL3（`dispatch.launch`はL2）のため、この
# モジュールは`claim_task`を直接importしない（`tests/test_architecture.py`の
# レイヤー境界を参照）。呼び出し元のL3層（`orchestune.dispatch.cycle_actions`）
# が`claim_task`を束縛した`ClaimFn`をDIで渡す。第2引数は`plan.base_branch_for_launch`
# 由来のdefault_base。
ClaimFn = Callable[[ClaimRequest, str], ClaimOutcome]


def _is_task_stack_eligible(
    task: TaskMetadata, view: DependencyPolicyView
) -> StackDecision:
    """Delegate launch eligibility and base selection to the shared policy."""
    return decide_stack_target(task.issue_number, view)


def _get_stack_eligible_tasks(
    tasks: Sequence[TTask], view: DependencyPolicyView
) -> tuple[list[TTask], dict[int, str]]:
    stack_eligible_tasks = []
    task_to_base_branch = {}

    for task in sorted(tasks, key=lambda candidate: candidate.issue_number):
        if not task.subtask_id:
            continue
        if StatusLabel.IN_PROGRESS in task.status_labels:
            continue
        decision = _is_task_stack_eligible(task, view)
        if decision.target is None:
            continue
        stack_eligible_tasks.append(task)
        task_to_base_branch[task.issue_number] = decision.target.branch

    return stack_eligible_tasks, task_to_base_branch


@dataclass
class DuplicateCandidateDecision(Generic[TTask]):
    task: TTask
    is_duplicate: bool
    existing_pr: PrRecord | None = None


def _find_existing_pr_for_task(
    task: TaskMetadata, view: CycleQueries
) -> PrRecord | None:
    resolution = view.branch_resolution(task.issue_number)
    if resolution is None or not resolution.allows(BranchCapability.LINK_PR):
        return None
    return resolution.pr


def _is_pr_duplicate_update(
    existing_pr: PrRecord,
    task: TaskMetadata,
    completed_worktrees: list[CompletedWorktree],
) -> bool:
    last_completed = None
    for cw in reversed(completed_worktrees):
        if cw.issue_number == task.issue_number:
            last_completed = cw
            break

    if not last_completed:
        return True

    remote_sha = None
    ls_remote_failed = False
    try:
        ref_name = f"refs/heads/{existing_pr.head_ref}"
        res = run_git(["ls-remote", "origin", ref_name], cwd=None, check=True)
        output = res.stdout.strip()
        if output:
            remote_sha = output.split()[0]
    except Exception:
        ls_remote_failed = True

    if ls_remote_failed:
        return True
    if last_completed.commit_sha and remote_sha:
        if last_completed.commit_sha != remote_sha:
            return True
    return False


def _decide_duplicate_candidates(
    candidate_tasks: Sequence[TTask],
    view: CycleQueries,
    completed_worktrees: list[CompletedWorktree] | None = None,
) -> list[DuplicateCandidateDecision[TTask]]:
    decisions = []
    for task in candidate_tasks:
        existing_pr = _find_existing_pr_for_task(task, view)
        is_duplicate = False
        if existing_pr:
            is_duplicate = _is_pr_duplicate_update(
                existing_pr, task, completed_worktrees or []
            )
        decisions.append(
            DuplicateCandidateDecision(
                task=task, is_duplicate=is_duplicate, existing_pr=existing_pr
            )
        )
    return decisions


def _apply_duplicate_skip(
    decisions: Sequence[DuplicateCandidateDecision[TTask]],
    config: DispatcherConfig,
) -> list[TTask]:
    """decide層が判定した重複候補をstatus:blocked-human-reviewへ遷移させ、
    重複でないタスクのみを起動候補として返す。"""
    valid_candidate_tasks = []
    for decision in decisions:
        task = decision.task
        if decision.is_duplicate and decision.existing_pr:
            existing_pr = decision.existing_pr
            print(
                f"Skipping task {task.subtask_id} (Issue #{task.issue_number}) because an open PR #{existing_pr.number} already exists on branch '{existing_pr.head_ref}' and has been updated.",
                file=sys.stderr,
            )
            if config.apply:
                apply_human_review_escalation(
                    task.issue_number,
                    task.status_labels,
                    f"重複起動防止: このサブタスクに対応するオープンなPR #{existing_pr.number} (ブランチ: `{existing_pr.head_ref}`) が既に検出され、更新されています。\n"
                    f"重複したエージェントセッションの起動を防ぐため、自動起動をスキップし、ステータスを `status:blocked-human-review` に変更しました。\n"
                    f"必要に応じて手動でPRをマージするか、再起動したい場合は既存のPRをクローズした上で再度 `status:queued` に設定してください。",
                    forge=config.resolved_forge,
                )
        else:
            valid_candidate_tasks.append(task)
    return valid_candidate_tasks


@dataclass
class TaskLaunchPlan(Generic[TTask]):
    task: TTask
    branch_name: str
    base_branch_for_launch: str | None
    base_branch_for_state: str
    execution_selection: ExecutionSelection | None = None


def _decide_yaml_error_tasks(candidate_tasks: Sequence[TTask]) -> list[TTask]:
    """YAMLパースに失敗しているタスクを判定する（副作用なし）。"""
    return [task for task in candidate_tasks if task.yaml_error]


def _apply_yaml_error_blocking(
    yaml_error_tasks: Sequence[TaskMetadata], config: DispatcherConfig
) -> None:
    for task in yaml_error_tasks:
        transition_status_label(
            config.resolved_forge,
            task.issue_number,
            StatusLabel.BLOCKED,
            (StatusLabel.QUEUED,),
        )
        config.resolved_forge.add_comment(
            task.issue_number,
            "YAMLのパースに失敗したため、タスクをブロックしました。フォーマットを確認してください。",
        )


def _decide_task_launch_plan(
    selected: Sequence[TTask],
    task_to_base_branch: dict[int, str],
    config: DispatcherConfig,
) -> list[TaskLaunchPlan[TTask]]:
    """選出されたタスクごとに、起動時のブランチ名・ベースブランチを決定する（副作用なし）。"""
    plans = []
    for task in selected:
        branch_name = build_task_branch_name(task.issue_number, task.subtask_id)
        base_branch = task_to_base_branch.get(task.issue_number)
        if base_branch is None:
            if config.parent_issue_number is not None:
                base_branch_for_launch = f"parent/issue-{config.parent_issue_number}"
                base_branch_for_state = base_branch_for_launch
            else:
                base_branch_for_launch = None
                base_branch_for_state = "origin/main"
        else:
            base_branch_for_launch = base_branch
            base_branch_for_state = base_branch

        execution_selection = resolve_task_execution_selection(task, config)

        plans.append(
            TaskLaunchPlan(
                task=task,
                branch_name=branch_name,
                base_branch_for_launch=base_branch_for_launch,
                base_branch_for_state=base_branch_for_state,
                execution_selection=execution_selection,
            )
        )
    return plans


def _persist_launch_history(now: float, config: DispatcherConfig) -> None:
    """#514: 今回の起動タイムスタンプを親Issue本文へ起動前に追記する（スロット予約）。"""
    if config.parent_issue_number is None:
        return
    issue = config.resolved_forge.get_issue(config.parent_issue_number)
    if issue is None:
        return
    merged = launch_history_in_window(
        launch_history_from_body(issue.body), now, config.window_seconds
    )
    merged.append(now)
    patched_body = backfill_launch_history(issue.body, sorted(merged))
    if patched_body is not None:
        config.resolved_forge.update_issue_body(
            config.parent_issue_number, patched_body
        )


def _release_launch_reservation(now: float, config: DispatcherConfig) -> None:
    """#519: _persist_launch_historyで確保した予約を1件分解放する。"""
    if config.parent_issue_number is None:
        return
    try:
        issue = config.resolved_forge.get_issue(config.parent_issue_number)
        if issue is None:
            return
        remaining = launch_history_from_body(issue.body)
        if now not in remaining:
            return
        remaining.remove(now)
        patched_body = backfill_launch_history(issue.body, sorted(remaining))
        if patched_body is not None:
            config.resolved_forge.update_issue_body(
                config.parent_issue_number, patched_body
            )
    except Exception as e:
        print(
            f"Warning: failed to release the launch reservation in parent issue "
            f"#{config.parent_issue_number}: {e}",
            file=sys.stderr,
        )


@contextmanager
def _launch_reservation(
    now: float, config: DispatcherConfig, issue_number: int | None = None
) -> Iterator[Callable[[], None] | None]:
    try:
        _persist_launch_history(now, config)
    except Exception as e:
        target = f"issue #{issue_number}" if issue_number is not None else "task"
        print(
            f"Warning: skipping launch of {target}: failed to "
            f"reserve a launch slot in parent issue "
            f"#{config.parent_issue_number}: {e}",
            file=sys.stderr,
        )
        yield None
        return

    committed = False

    def commit() -> None:
        nonlocal committed
        committed = True

    try:
        yield commit
    finally:
        if not committed:
            _release_launch_reservation(now, config)


def _handle_launch_failure(
    task: TaskMetadata, launch, config: DispatcherConfig
) -> None:
    old_labels = tuple(
        label
        for label in (StatusLabel.QUEUED, StatusLabel.BLOCKED)
        if label in task.status_labels
    )
    if launch.validation_error:
        transition_status_label(
            config.resolved_forge,
            task.issue_number,
            StatusLabel.BLOCKED_HUMAN_REVIEW,
            old_labels,
        )
        config.resolved_forge.add_comment(
            task.issue_number,
            f"ブランチ名またはsubtask_idが不正なため、タスクをブロックしました (`status:blocked-human-review`)。\n"
            f"エラー内容:\n```\n{launch.error_message}\n```",
        )
    else:
        transition_status_label(
            config.resolved_forge,
            task.issue_number,
            StatusLabel.BLOCKED,
            old_labels,
        )
        config.resolved_forge.add_comment(
            task.issue_number,
            f"Git worktreeの作成またはエージェントの起動に失敗しました。\n"
            f"エラー内容:\n```\n{launch.error_message}\n```",
        )


def _build_active_worktree_from_launch(
    task: TaskMetadata,
    plan: TaskLaunchPlan,
    launch,
    run_state: RunState,
    now: float,
) -> ActiveWorktree:
    selection = plan.execution_selection
    profile = selection.profile if selection else task.execution_profile
    model = selection.model if selection else None
    reasoning_effort = selection.reasoning_effort if selection else None
    selection_reason = selection.reason if selection else None

    return ActiveWorktree(
        issue_number=task.issue_number,
        branch=launch.branch,
        worktree_path=launch.worktree_path,
        pid=launch.pid,
        started_at=launch.dispatch_started_at or now,
        declared_footprint=task.footprint,
        external_id=launch.external_id,
        external_url=launch.external_url,
        base_branch=launch.base_ref or plan.base_branch_for_state,
        estimated_tokens=build_cost_model(run_state).tokens_for_issue(
            task.issue_number
        ),
        token_estimate_recorded=True,
        profile=profile,
        model=model,
        reasoning_effort=reasoning_effort,
        selection_reason=selection_reason,
        launch_attempt_id=launch.launch_attempt_id,
        launch_phase="launched" if launch.launch_attempt_id else None,
        # #943: dispatch launchはclaim_task（owner_kind=dispatch）経由で予約される。
        # `launch.reservation_kind`はclaim outcomeから`_try_planned_launch`が
        # 設定するが、未設定（None）の場合はActiveWorktreeの既定値
        # （reservation_kind="footprint"）へ安全にフォールバックする。
        owner_kind=OwnerKind.DISPATCH.value,
        claim_id=launch.claim_id,
        base_ref=launch.base_ref,
        reservation_kind=launch.reservation_kind or "footprint",
    )


def _record_successful_launch(
    task: TaskMetadata,
    plan: TaskLaunchPlan,
    launch,
    run_state: RunState,
    now: float,
    config: DispatcherConfig,
    open_prs: Sequence[PrRecord] | None,
    on_launch_committed: LaunchCommitted | None = None,
) -> None:
    active = _build_active_worktree_from_launch(task, plan, launch, run_state, now)
    run_state.active_worktrees[str(task.issue_number)] = active
    run_state.launch_history.append(now)
    reclaim_record = run_state.task_reclaim_counts.get(task.issue_number)
    if reclaim_record is not None and reclaim_record.pending:
        reclaim_record.pending = False
    if reclaim_record is not None and reclaim_record.early_death_retry_pending:
        reclaim_record.early_death_retry_pending = False
    save_run_state(
        run_state,
        config.run_state_path,
        now=now,
        launch_window_seconds=config.window_seconds,
        open_prs=open_prs,
    )
    if on_launch_committed is not None:
        on_launch_committed(active)
    transition_status_label(
        config.resolved_forge,
        task.issue_number,
        StatusLabel.IN_PROGRESS,
        (
            label
            for label in (StatusLabel.QUEUED, StatusLabel.BLOCKED)
            if label in task.status_labels
        ),
    )


def _resolve_claim_failure_launch_result(
    plan: TaskLaunchPlan[TTask], outcome: ClaimOutcome
) -> LaunchResult | None:
    """claim_taskの失敗outcomeを、既存のLaunchResultベースの失敗処理へ変換する。

    STATE_LOCK_FAILEDは一時的な競合であり、`LaunchOutcomeUnknown`と同様に
    次サイクルへ持ち越す（Noneを返す）。それ以外の拒否理由は、branch/subtask_id
    の不正（`INVALID_BRANCH_NAME`）だけを`validation_error`として
    `status:blocked-human-review`へ、それ以外（`WORKTREE_CREATION_FAILED`を
    含む、OSError/git実行エラーのような一時的なインフラ障害や所有権拒否）は
    再試行可能な`status:blocked`へ振り分ける既存の`_handle_launch_failure`
    分岐へそのまま乗せる（#943レビュー対応(Codex P2):
    `WORKTREE_CREATION_FAILED`を一律`validation_error`扱いすると、一時的な
    worktree作成失敗まで恒久的な`status:blocked-human-review`へ誤って
    エスカレーションしてしまうため分離した）。

    #943: `outcome.stage is ClaimStage.ACTIVE_SAVED`は、claim_task内部の
    finalize段階（issue再検証・所有権メタデータ公開・ラベル遷移・完了保存の
    いずれか）で失敗したことを意味する——つまりclaim_task自身が既に
    `status:in-progress`への遷移を試み、場合によっては部分的に成功させて
    いる可能性がある。この場合に`_handle_launch_failure`の汎用フォールバック
    （dispatch自身が起動前のstale統な`task.status_labels`を元に、もう一度
    独立した`transition_status_label`を試みる）を重ねて適用すると、claimが
    既に付与済みの`status:in-progress`の上へさらに`status:blocked`を重ねて
    付与し、`status:queued`の除去も二重に試行してしまう——ラベルが混在した
    まま矛盾した状態になりかねない。claim_task自身の設計
    （「ラベル更新API失敗時に予約とworktreeが保持され、再選出させない」）が
    既に安全側に倒しているため、この段階の失敗は次サイクルの整合性回復
    （reconciliation）に委ね、dispatch側で重ねて手を加えない。
    """
    failure = outcome.failure
    assert failure is not None
    if (
        failure.reason is ClaimFailureReason.STATE_LOCK_FAILED
        or outcome.stage is ClaimStage.ACTIVE_SAVED
    ):
        print(
            f"Holding launch of issue #{plan.task.issue_number}: {failure.message}; retry next cycle",
            file=sys.stderr,
        )
        return None
    return LaunchResult(
        issue_number=plan.task.issue_number,
        branch=plan.branch_name,
        worktree_path="",
        pid=None,
        launched=False,
        error_message=failure.message,
        validation_error=failure.reason is ClaimFailureReason.INVALID_BRANCH_NAME,
        execution_selection=plan.execution_selection,
    )


def _sync_claim_reservation_into_run_state(
    run_state: RunState, config: DispatcherConfig, issue_number: int
) -> None:
    """#943: claim_taskが自前のload/save往復でdiskへ直接永続化した予約を、
    サイクルが保持し続ける共有run_stateオブジェクトへも反映する。

    反映しないと、後続のagent起動が失敗した場合に`_apply_task_launches`末尾の
    `save_run_state`（サイクル全体で保持している古いin-memoryスナップショット）が
    claimの予約を上書き消去してしまう——GitHub上は`status:in-progress`なのに
    run_state.jsonにactiveエントリが無い孤児状態が生まれる。
    """
    fresh = load_run_state(config.run_state_path)
    key = str(issue_number)
    reservation = fresh.active_worktrees.get(key)
    if reservation is not None:
        run_state.active_worktrees[key] = reservation


def _try_planned_launch(
    plan: TaskLaunchPlan[TTask],
    target: DispatchTarget,
    config: DispatcherConfig,
    run_state: RunState,
    claim_fn: ClaimFn,
) -> LaunchResult | None:
    """#943: worktree/所有権の取得をclaim_task（owner_kind=dispatch）経由に一本化する。

    実際のagentプロセス起動（`_launch_on_prepared_worktree`）はworktree.py側の
    既存実装をそのまま再利用し、claim_taskが用意したworktree_path/branch/base_ref
    に対して行う。dispatch固有の責務（quota起動履歴・launch_attempt・target設定）は
    このモジュール（`_apply_task_launches`/`launch_attempts.py`）に残す。

    `claim_fn`はL3層（`orchestune.dispatch.cycle_actions`）が`claim_task`を
    束縛して渡すDI境界（このモジュールの`ClaimFn`定義を参照）。
    """
    task = plan.task
    request = ClaimRequest(
        issue_number=task.issue_number,
        owner_kind=OwnerKind.DISPATCH,
        state_path=config.run_state_path,
    )
    outcome = claim_fn(request, plan.base_branch_for_launch or "origin/main")
    if outcome.claim_id is not None:
        _sync_claim_reservation_into_run_state(run_state, config, task.issue_number)

    if not outcome.success:
        return _resolve_claim_failure_launch_result(plan, outcome)

    assert outcome.worktree_path is not None
    assert outcome.branch is not None
    try:
        launch = _launch_on_prepared_worktree(
            task,
            outcome.branch,
            outcome.worktree_path,
            target,
            outcome.base_ref,
            execution_selection=plan.execution_selection,
        )
    except LaunchOutcomeUnknown as exc:
        print(
            f"Holding launch of issue #{plan.task.issue_number}: {exc}; reconcile durable attempt on next cycle",
            file=sys.stderr,
        )
        return None

    launch.base_ref = outcome.base_ref
    launch.claim_id = outcome.claim_id
    launch.reservation_kind = (
        outcome.reservation_kind.value if outcome.reservation_kind else None
    )
    return launch


def _apply_single_task_launch(
    plan: TaskLaunchPlan[TTask],
    run_state: RunState,
    now: float,
    config: DispatcherConfig,
    claim_fn: ClaimFn,
    open_prs: Sequence[PrRecord] | None,
    on_launch_committed: LaunchCommitted | None,
) -> TTask | None:
    """1件のplanに対する予約→起動→記録の一連の流れ。成功時のみtaskを返す。"""
    task = plan.task
    assert config.dispatch_target is not None

    with _launch_reservation(
        now, config, issue_number=task.issue_number
    ) as commit_reservation:
        if commit_reservation is None:
            return None

        target = prepare_journaled_target(
            plan, run_state, now, config, commit_reservation
        )
        if target is None:
            return None

        launch = _try_planned_launch(plan, target, config, run_state, claim_fn)
        if launch is None:
            run_state.launch_history.append(now)
            return None
        if not launch.launched:
            _handle_launch_failure(task, launch, config)
            return None

        commit_reservation()
        _record_successful_launch(
            task, plan, launch, run_state, now, config, open_prs, on_launch_committed
        )
        return task


def _apply_task_launches(
    plans: Sequence[TaskLaunchPlan[TTask]],
    run_state: RunState,
    now: float,
    config: DispatcherConfig,
    open_prs: Sequence[PrRecord] | None = None,
    on_launch_committed: LaunchCommitted | None = None,
    *,
    claim_fn: ClaimFn,
) -> list[TTask]:
    actually_selected = [
        task
        for plan in plans
        if (
            task := _apply_single_task_launch(
                plan, run_state, now, config, claim_fn, open_prs, on_launch_committed
            )
        )
        is not None
    ]

    save_run_state(
        run_state,
        config.run_state_path,
        now=now,
        launch_window_seconds=config.window_seconds,
        open_prs=open_prs,
    )
    return actually_selected


@dataclass
class LaunchContext(Generic[TTask]):
    """#476: `_launch_selected_tasks`の7引数を集約するDTO。"""

    selected: Sequence[TTask]
    task_to_base_branch: dict[int, str]
    candidate_tasks: Sequence[TTask]
    run_state: RunState
    now: float
    config: DispatcherConfig
    open_prs: Sequence[PrRecord] | None = None
    on_launch_committed: LaunchCommitted | None = None
    # #943: `orchestune.claim.service`はL3のため、`claim_task`を束縛した
    # `ClaimFn`をL3層（`orchestune.dispatch.cycle_actions`）からDIで受け取る
    # （このモジュールの`ClaimFn`定義を参照）。
    claim_fn: ClaimFn | None = None


def _launch_selected_tasks(ctx: LaunchContext[TTask]) -> list[TTask]:
    """decide+applyの薄いラッパー（呼び出し互換のため維持）。"""
    yaml_error_tasks = _decide_yaml_error_tasks(ctx.candidate_tasks)
    _apply_yaml_error_blocking(yaml_error_tasks, ctx.config)

    plans = _decide_task_launch_plan(ctx.selected, ctx.task_to_base_branch, ctx.config)
    assert ctx.claim_fn is not None
    return _apply_task_launches(
        plans,
        ctx.run_state,
        ctx.now,
        ctx.config,
        open_prs=ctx.open_prs,
        on_launch_committed=ctx.on_launch_committed,
        claim_fn=ctx.claim_fn,
    )
