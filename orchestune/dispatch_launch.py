"""起動候補の選定（スタック可否判定・重複起動防止）と、選出タスクの実起動。"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING

from orchestune import github
from orchestune.dispatch_scoring import Task, parse_task_from_issue
from orchestune.dispatch_state import ActiveWorktree, RunState, save_run_state
from orchestune.dispatch_worktree import create_worktree_and_launch
from orchestune.github import IssueRecord, PrRecord

if TYPE_CHECKING:
    from orchestune.dispatcher import DispatcherConfig


def _get_stack_eligible_tasks(
    blocked_issues: list[IssueRecord],
    tasks_by_issue: dict[int, Task],
    done_subtask_ids: set[str],
    ci_passed_pr_subtask_ids: set[str],
    subtask_branch_map: dict[str, str],
) -> tuple[list[Task], dict[int, str]]:
    stack_eligible_tasks = []
    task_to_base_branch = {}

    for issue in blocked_issues:
        task = parse_task_from_issue(issue)
        if not task.subtask_id or not task.depends_on:
            continue

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
                if dep_task:
                    if not all(
                        grand_dep in done_subtask_ids
                        for grand_dep in dep_task.depends_on
                    ):
                        all_resolved_or_stackable = False
                        break
                stackable_deps.append(dep)
            else:
                all_resolved_or_stackable = False
                break

        # スタッキング可能な未マージ依存先が「ちょうど1つ」の場合のみスタッキング起動を許可する
        # （複数ある場合は、両方の変更をベースブランチとして同時に取り込めないためマージされるまでブロックする）
        if all_resolved_or_stackable and len(stackable_deps) == 1:
            stack_eligible_tasks.append(task)
            dep = stackable_deps[0]
            task_to_base_branch[task.issue_number] = subtask_branch_map[dep]

    return stack_eligible_tasks, task_to_base_branch


@dataclass
class DuplicateCandidateDecision:
    task: Task
    is_duplicate: bool
    existing_pr: PrRecord | None = None


def _decide_duplicate_candidates(
    candidate_tasks: list[Task],
    pr_by_branch: dict[str, PrRecord],
    prs: list[PrRecord],
    run_state: RunState,
) -> list[DuplicateCandidateDecision]:
    """既にオープンなPRが存在するcandidate_tasksを検知し、重複起動かどうかを判断する
    （githubラベル等の書き換えは行わない。`git ls-remote`は読み取り専用）。"""
    decisions = []
    for task in candidate_tasks:
        expected_branch = f"claude/issue-{task.issue_number}-{task.subtask_id}"

        existing_pr = pr_by_branch.get(expected_branch)
        if not existing_pr:
            for pr in prs:
                if task.issue_number in pr.closes_issue_numbers:
                    existing_pr = pr
                    break

        is_duplicate = False
        if existing_pr:
            # 重複起動をスキップする条件の判定
            last_completed = None
            for cw in reversed(run_state.completed_worktrees):
                if cw.issue_number == task.issue_number:
                    last_completed = cw
                    break

            if not last_completed:
                # 過去の完了履歴がないのにPRが存在する場合は、人間が作成したとみなしてスキップ
                is_duplicate = True
            else:
                # リモートブランチの最新コミットSHAを取得
                remote_sha = None
                ls_remote_failed = False
                try:
                    ref_name = f"refs/heads/{existing_pr.head_ref}"
                    res = subprocess.run(
                        ["git", "ls-remote", "origin", ref_name],
                        capture_output=True,
                        text=True,
                        check=True,
                    )
                    output = res.stdout.strip()
                    if output:
                        remote_sha = output.split()[0]
                except Exception:
                    ls_remote_failed = True

                # 履歴のSHAとリモートのSHAが両方取得でき、かつそれらが異なる場合のみ重複（人間介入）と判定。
                # ただし、ls-remoteが例外等で失敗した場合は、安全のため重複とみなして起動をスキップする。
                if ls_remote_failed:
                    is_duplicate = True
                elif last_completed.commit_sha and remote_sha:
                    if last_completed.commit_sha != remote_sha:
                        is_duplicate = True

        decisions.append(
            DuplicateCandidateDecision(
                task=task, is_duplicate=is_duplicate, existing_pr=existing_pr
            )
        )
    return decisions


def _apply_duplicate_skip(
    decisions: list[DuplicateCandidateDecision],
    config: DispatcherConfig,
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
            if config.apply:
                if "status:queued" in task.status_labels:
                    github.remove_label(task.issue_number, "status:queued")
                if "status:blocked" in task.status_labels:
                    github.remove_label(task.issue_number, "status:blocked")
                github.add_label(task.issue_number, "status:blocked-human-review")
                github.add_comment(
                    task.issue_number,
                    f"重複起動防止: このサブタスクに対応するオープンなPR #{existing_pr.number} (ブランチ: `{existing_pr.head_ref}`) が既に検出され、更新されています。\n"
                    f"重複したエージェントセッションの起動を防ぐため、自動起動をスキップし、ステータスを `status:blocked-human-review` に変更しました。\n"
                    f"必要に応じて手動でPRをマージするか、再起動したい場合は既存のPRをクローズした上で再度 `status:queued` に設定してください。",
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


def _apply_yaml_error_blocking(yaml_error_tasks: list[Task]) -> None:
    for task in yaml_error_tasks:
        github.remove_label(task.issue_number, "status:queued")
        github.add_label(task.issue_number, "status:blocked")
        github.add_comment(
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


def _apply_task_launches(
    plans: list[TaskLaunchPlan],
    run_state: RunState,
    now: float,
    config: DispatcherConfig,
) -> list[Task]:
    """decide層が立てた起動計画に基づき、worktree作成・エージェント（LLM）の実起動
    ・run_state/githubラベルの更新を行う。"""
    actually_selected = []
    for plan in plans:
        task = plan.task
        assert config.dispatch_target is not None
        launch = create_worktree_and_launch(
            task,
            plan.branch_name,
            config.worktree_root,
            config.dispatch_target,
            apply=True,
            base_branch=plan.base_branch_for_launch,
        )
        if not launch.launched:
            if "status:queued" in task.status_labels:
                github.remove_label(task.issue_number, "status:queued")
            if "status:blocked" in task.status_labels:
                github.remove_label(task.issue_number, "status:blocked")
            github.add_label(task.issue_number, "status:blocked")
            github.add_comment(
                task.issue_number,
                f"Git worktreeの作成またはエージェントの起動に失敗しました。\n"
                f"エラー内容:\n```\n{launch.error_message}\n```",
            )
            continue

        # run_stateへの登録・永続化を先に行い、GitHubラベルの更新は後で行う。
        # 起動(create_worktree_and_launch)は既に成功しているため、この順序なら
        # この後でクラッシュしても「run_stateには記録済みだがGitHubラベルは
        # まだstatus:queuedのまま」という、次回サイクルの冒頭でラベルを見て
        # 機械的に検出・破棄できる非対称にしかならない（逆順だと「GitHub側は
        # 確定・run_state側は空」という検出不能な非対称になってしまう）。
        run_state.active_worktrees[str(task.issue_number)] = ActiveWorktree(
            issue_number=task.issue_number,
            branch=plan.branch_name,
            worktree_path=launch.worktree_path,
            pid=launch.pid,
            started_at=now,
            declared_footprint=task.footprint,
            external_id=launch.external_id,
            external_url=launch.external_url,
            base_branch=plan.base_branch_for_state,
        )
        run_state.launch_history.append(now)
        save_run_state(run_state, config.run_state_path)

        if "status:queued" in task.status_labels:
            github.remove_label(task.issue_number, "status:queued")
        if "status:blocked" in task.status_labels:
            github.remove_label(task.issue_number, "status:blocked")
        github.add_label(task.issue_number, "status:in-progress")
        actually_selected.append(task)

    save_run_state(run_state, config.run_state_path)
    return actually_selected


def _launch_selected_tasks(
    selected: list[Task],
    task_to_base_branch: dict[int, str],
    candidate_tasks: list[Task],
    run_state: RunState,
    now: float,
    config: DispatcherConfig,
) -> list[Task]:
    """decide+applyの薄いラッパー（呼び出し互換のため維持）。"""
    yaml_error_tasks = _decide_yaml_error_tasks(candidate_tasks)
    _apply_yaml_error_blocking(yaml_error_tasks)

    plans = _decide_task_launch_plan(selected, task_to_base_branch, config)
    return _apply_task_launches(plans, run_state, now, config)
