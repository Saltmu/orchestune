"""起動候補の選定（スタック可否判定・重複起動防止）と、選出タスクの実起動。"""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from orchestune.dispatch_escalation import apply_human_review_escalation
from orchestune.dispatch_labels import transition_status_label
from orchestune.dispatch_scoring import Task, parse_task_from_issue
from orchestune.dispatch_state import ActiveWorktree, RunState, save_run_state
from orchestune.dispatch_worktree import create_worktree_and_launch
from orchestune.git_cli import run_git
from orchestune.issue_parsing import (
    backfill_launch_history,
    launch_history_from_body,
    launch_history_in_window,
)
from orchestune.models import IssueRecord, PrRecord

if TYPE_CHECKING:
    from orchestune.dispatch_config import DispatcherConfig
    from orchestune.dispatch_rules import CycleContext


def _is_task_stack_eligible(
    task: Task,
    tasks_by_issue: dict[int, Task],
    done_subtask_ids: set[str],
    ci_passed_pr_subtask_ids: set[str],
    resolved_grand_deps: set[str],
) -> tuple[bool, list[str]]:
    all_resolved_or_stackable = True
    stackable_deps = []
    for dep in task.depends_on:
        if dep in done_subtask_ids:
            continue
        elif dep in ci_passed_pr_subtask_ids:
            dep_task = None
            for t in tasks_by_issue.values():
                if t.subtask_id == dep:
                    dep_task = t
                    break
            if dep_task and not all(
                grand_dep in resolved_grand_deps for grand_dep in dep_task.depends_on
            ):
                all_resolved_or_stackable = False
                break
            stackable_deps.append(dep)
        else:
            all_resolved_or_stackable = False
            break
    return all_resolved_or_stackable, stackable_deps


def _get_stack_eligible_tasks(
    blocked_issues: list[IssueRecord],
    tasks_by_issue: dict[int, Task],
    done_subtask_ids: set[str],
    ci_passed_pr_subtask_ids: set[str],
    subtask_branch_map: dict[str, str],
    completed_subtask_ids: set[str] | None = None,
) -> tuple[list[Task], dict[int, str]]:
    stack_eligible_tasks = []
    task_to_base_branch = {}
    resolved_grand_deps = done_subtask_ids | (completed_subtask_ids or set())

    for issue in blocked_issues:
        task = tasks_by_issue.get(issue.number) or parse_task_from_issue(issue)
        if not task.subtask_id or not task.depends_on:
            continue
        if "status:in-progress" in task.status_labels:
            continue
        if issue.blocked_by and len(task.depends_on) < len(issue.blocked_by):
            continue

        all_ok, stackable_deps = _is_task_stack_eligible(
            task,
            tasks_by_issue,
            done_subtask_ids,
            ci_passed_pr_subtask_ids,
            resolved_grand_deps,
        )
        if all_ok and len(stackable_deps) == 1:
            stack_eligible_tasks.append(task)
            dep = stackable_deps[0]
            task_to_base_branch[task.issue_number] = subtask_branch_map[dep]

    return stack_eligible_tasks, task_to_base_branch


@dataclass
class DuplicateCandidateDecision:
    task: Task
    is_duplicate: bool
    existing_pr: PrRecord | None = None


def _is_orchestune_issue_branch(head_ref: str, issue_number: int) -> bool:
    """PR本文のCloses一致フォールバックをOrchestune由来らしいブランチに限定する。"""
    return head_ref.startswith(f"claude/issue-{issue_number}-")


def _find_existing_pr_for_task(task: Task, ctx: CycleContext) -> PrRecord | None:
    expected_branch = f"claude/issue-{task.issue_number}-{task.subtask_id}"
    existing_pr = ctx.pr_by_branch.get(expected_branch)
    if not existing_pr:
        for pr in ctx.prs:
            if (
                task.issue_number in pr.closes_issue_numbers
                and _is_orchestune_issue_branch(pr.head_ref, task.issue_number)
            ):
                return pr
    return existing_pr


def _is_pr_duplicate_update(
    existing_pr: PrRecord, task: Task, ctx: CycleContext
) -> bool:
    last_completed = None
    for cw in reversed(ctx.run_state.completed_worktrees):
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
    candidate_tasks: list[Task],
    ctx: CycleContext,
) -> list[DuplicateCandidateDecision]:
    decisions = []
    for task in candidate_tasks:
        existing_pr = _find_existing_pr_for_task(task, ctx)
        is_duplicate = False
        if existing_pr:
            is_duplicate = _is_pr_duplicate_update(existing_pr, task, ctx)
        decisions.append(
            DuplicateCandidateDecision(
                task=task, is_duplicate=is_duplicate, existing_pr=existing_pr
            )
        )
    return decisions


def _apply_duplicate_skip(
    decisions: list[DuplicateCandidateDecision],
    ctx: CycleContext,
) -> list[Task]:
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
            if ctx.config.apply:
                apply_human_review_escalation(
                    task.issue_number,
                    task.status_labels,
                    f"重複起動防止: このサブタスクに対応するオープンなPR #{existing_pr.number} (ブランチ: `{existing_pr.head_ref}`) が既に検出され、更新されています。\n"
                    f"重複したエージェントセッションの起動を防ぐため、自動起動をスキップし、ステータスを `status:blocked-human-review` に変更しました。\n"
                    f"必要に応じて手動でPRをマージするか、再起動したい場合は既存のPRをクローズした上で再度 `status:queued` に設定してください。",
                    forge=ctx.config.resolved_forge,
                )
        else:
            valid_candidate_tasks.append(task)
    return valid_candidate_tasks


@dataclass
class TaskLaunchPlan:
    task: Task
    branch_name: str
    base_branch_for_launch: str | None
    base_branch_for_state: str


def _decide_yaml_error_tasks(candidate_tasks: list[Task]) -> list[Task]:
    """YAMLパースに失敗しているタスクを判定する（副作用なし）。"""
    return [task for task in candidate_tasks if task.yaml_error]


def _apply_yaml_error_blocking(
    yaml_error_tasks: list[Task], config: DispatcherConfig
) -> None:
    for task in yaml_error_tasks:
        transition_status_label(
            config.resolved_forge,
            task.issue_number,
            "status:blocked",
            ("status:queued",),
        )
        config.resolved_forge.add_comment(
            task.issue_number,
            "YAMLのパースに失敗したため、タスクをブロックしました。フォーマットを確認してください。",
        )


def _decide_task_launch_plan(
    selected: list[Task],
    task_to_base_branch: dict[int, str],
    config: DispatcherConfig,
) -> list[TaskLaunchPlan]:
    """選出されたタスクごとに、起動時のブランチ名・ベースブランチを決定する（副作用なし）。"""
    plans = []
    for task in selected:
        branch_name = f"claude/issue-{task.issue_number}-{task.subtask_id or 'task'}"
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

        plans.append(
            TaskLaunchPlan(
                task=task,
                branch_name=branch_name,
                base_branch_for_launch=base_branch_for_launch,
                base_branch_for_state=base_branch_for_state,
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


def _handle_launch_failure(task: Task, launch, config: DispatcherConfig) -> None:
    old_labels = tuple(
        label
        for label in ("status:queued", "status:blocked")
        if label in task.status_labels
    )
    if launch.validation_error:
        transition_status_label(
            config.resolved_forge,
            task.issue_number,
            "status:blocked-human-review",
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
            "status:blocked",
            old_labels,
        )
        config.resolved_forge.add_comment(
            task.issue_number,
            f"Git worktreeの作成またはエージェントの起動に失敗しました。\n"
            f"エラー内容:\n```\n{launch.error_message}\n```",
        )


def _record_successful_launch(
    task: Task,
    plan: TaskLaunchPlan,
    launch,
    run_state: RunState,
    now: float,
    config: DispatcherConfig,
    open_prs: Sequence[PrRecord] | None,
) -> None:
    run_state.active_worktrees[str(task.issue_number)] = ActiveWorktree(
        issue_number=task.issue_number,
        branch=plan.branch_name,
        worktree_path=launch.worktree_path,
        pid=launch.pid,
        started_at=launch.dispatch_started_at or now,
        declared_footprint=task.footprint,
        external_id=launch.external_id,
        external_url=launch.external_url,
        base_branch=plan.base_branch_for_state,
    )
    run_state.launch_history.append(now)
    reclaim_record = run_state.task_reclaim_counts.get(task.issue_number)
    if reclaim_record is not None and reclaim_record.pending:
        reclaim_record.pending = False
    save_run_state(
        run_state,
        config.run_state_path,
        now=now,
        launch_window_seconds=config.window_seconds,
        open_prs=open_prs,
    )
    transition_status_label(
        config.resolved_forge,
        task.issue_number,
        "status:in-progress",
        (
            label
            for label in ("status:queued", "status:blocked")
            if label in task.status_labels
        ),
    )


def _apply_task_launches(
    plans: list[TaskLaunchPlan],
    run_state: RunState,
    now: float,
    config: DispatcherConfig,
    open_prs: Sequence[PrRecord] | None = None,
) -> list[Task]:
    actually_selected = []
    for plan in plans:
        task = plan.task
        assert config.dispatch_target is not None

        with _launch_reservation(
            now, config, issue_number=task.issue_number
        ) as commit_reservation:
            if commit_reservation is None:
                continue

            launch = create_worktree_and_launch(
                task,
                plan.branch_name,
                config.worktree_root,
                config.dispatch_target,
                apply=True,
                base_branch=plan.base_branch_for_launch,
            )
            if not launch.launched:
                _handle_launch_failure(task, launch, config)
                continue

            commit_reservation()
            _record_successful_launch(
                task, plan, launch, run_state, now, config, open_prs
            )
            actually_selected.append(task)

    save_run_state(
        run_state,
        config.run_state_path,
        now=now,
        launch_window_seconds=config.window_seconds,
        open_prs=open_prs,
    )
    return actually_selected


@dataclass
class LaunchContext:
    """#476: `_launch_selected_tasks`の7引数を集約するDTO。"""

    selected: list[Task]
    task_to_base_branch: dict[int, str]
    candidate_tasks: list[Task]
    run_state: RunState
    now: float
    config: DispatcherConfig
    open_prs: Sequence[PrRecord] | None = None


def _launch_selected_tasks(ctx: LaunchContext) -> list[Task]:
    """decide+applyの薄いラッパー（呼び出し互換のため維持）。"""
    yaml_error_tasks = _decide_yaml_error_tasks(ctx.candidate_tasks)
    _apply_yaml_error_blocking(yaml_error_tasks, ctx.config)

    plans = _decide_task_launch_plan(ctx.selected, ctx.task_to_base_branch, ctx.config)
    return _apply_task_launches(
        plans, ctx.run_state, ctx.now, ctx.config, open_prs=ctx.open_prs
    )
