"""tests/test_dispatch_gc_*.py群が共有するヘルパー・フィクスチャデータ。

test_dispatch_gc.py (1418行) を、ルール別・クリーンアップ別
(#479: git primitives / stale entry rules / completed rule / integration)
へ分割した際、各ファイルから共通利用される`_ctx`/`_active`/`_task`/`_issue`
をこのモジュールへ切り出した。`test_`で始まらないためpytestには収集されない。

#916以降、素の生成処理は`tests/dispatch_test_support.py`へ移し、本モジュールは
「GC系テストの既定値」（Issue 280・`status:not-needed`・`src/foo.py`の
footprint）への薄い上書きと、GC固有の実行ヘルパーだけを持つ。
"""

from collections.abc import Sequence

from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.gc.zombies import (
    ZombieOrTimeoutReclaim,
    _reclaim_candidate_from_command,
)
from orchestune.dispatch.phase_gc import (
    _gc_supervisor,
    _GcReclaimAdapter,
    run_gc_phase,
)
from orchestune.dispatch.rules import _RuleExecutionContext
from orchestune.dispatch.scoring import Task
from orchestune.dispatch.state import RunState
from orchestune.models import PrRecord
from tests.dispatch_test_support import make_footprint_issue as _issue
from tests.dispatch_test_support import (
    make_state_root,
    make_test_active_worktree,
    make_test_cycle_context,
    make_test_dispatcher_config,
    make_test_task,
)

tmp_path = make_state_root()

__all__ = [
    "_active",
    "_ctx",
    "_in_progress_task",
    "_issue",
    "_reclaim_active",
    "_rule_ctx",
    "_task",
    "decide_gc_reclaims",
    "run_gc_reclaims",
]


def run_gc_reclaims(
    run_state: RunState,
    tasks_by_issue: dict[int, Task],
    config: DispatcherConfig,
    held_worktree_paths: set[str] | None = None,
    open_prs: Sequence[PrRecord] | None = None,
) -> list[dict]:
    hold_events = [
        {
            "action": "completion_skipped_dirty_worktree",
            "worktree_path": path,
        }
        for path in sorted(held_worktree_paths or ())
    ]
    outcome = run_gc_phase(
        run_state,
        tasks_by_issue,
        config,
        hold_events,
        open_prs=open_prs,
    )
    return outcome.completion_events[len(hold_events) :]


def decide_gc_reclaims(
    run_state: RunState,
    tasks_by_issue: dict[int, Task],
    config: DispatcherConfig,
    held_worktree_paths: set[str] | None,
    now: float,
    open_prs: Sequence[PrRecord] | None = None,
) -> list[ZombieOrTimeoutReclaim]:
    if not config.zombie_gc and config.task_timeout_seconds <= 0:
        return []
    held_paths = held_worktree_paths or set()
    held_issues = {
        active.issue_number
        for active in run_state.active_worktrees.values()
        if active.worktree_path in held_paths
    }
    adapter = _GcReclaimAdapter(
        run_state=run_state,
        tasks_by_issue=tasks_by_issue,
        config=config,
        open_prs=tuple(open_prs or ()),
        now=now,
    )
    scan = _gc_supervisor().full_scan("gc", observer=adapter, deriver=adapter)
    held_subjects = {str(issue_number) for issue_number in held_issues}
    active_by_subject = {
        str(active.issue_number): (key, active)
        for key, active in run_state.active_worktrees.items()
    }
    planned = (
        _reclaim_candidate_from_command(
            command,
            active_by_subject,
            tasks_by_issue,
            run_state,
            config.max_task_reclaims,
            now,
        )
        for command in scan.repair_candidates
        if command.subject_id not in held_subjects
    )
    return [reclaim for reclaim in planned if reclaim is not None]


def _ctx(*, forge=None, **overrides):
    """GC系テストの既定CycleContext（action portは未bind）。"""
    defaults = {"config": make_test_dispatcher_config(tmp_path, forge=forge)}
    defaults.update(overrides)
    return make_test_cycle_context(state_root=tmp_path, **defaults)


class _TestRuleContext(_RuleExecutionContext):
    """Low-level Rule input with convenient semantic-query forwarding."""

    def __getattr__(self, name):
        return getattr(self.queries, name)


def _rule_ctx(*, forge=None, **overrides):
    run_state = overrides.get("run_state", RunState(active_worktrees={}))
    tasks_by_issue = overrides.get("tasks_by_issue", {})
    prs = overrides.get("prs", [])
    config = overrides.get("config", make_test_dispatcher_config(tmp_path, forge=forge))
    ctx_overrides = {
        k: v
        for k, v in overrides.items()
        if k not in ("issue_number_by_subtask_id", "done_issue_numbers", "pr_by_branch")
    }
    query = _ctx(
        forge=forge,
        **{
            **ctx_overrides,
            "run_state": run_state,
            "tasks_by_issue": tasks_by_issue,
            "prs": prs,
            "config": config,
        },
    )
    return _TestRuleContext(
        run_state=run_state,
        queries=query,
        config=config,
        prs=tuple(prs),
        not_needed_review_dispatcher=query.not_needed_review_dispatcher,
        issue_records_by_number=overrides.get("issue_records_by_number", {}),
        tasks_by_issue=tasks_by_issue,
        issue_number_by_subtask_id=overrides.get("issue_number_by_subtask_id", {}),
    )


def _active(**overrides):
    """GC系テストの既定ActiveWorktree（Issue 280・生存中）。

    `branch`はIssue番号から導かず固定する。`issue_number`だけを差し替えて
    「run_state上のキーとIssue番号がずれている」状況を作るテストがあるため。
    """
    defaults = {
        "branch": "claude/issue-280-task-a",
        "declared_footprint": ("src/foo.py",),
    }
    defaults.update(overrides)
    return make_test_active_worktree(defaults.pop("issue_number", 280), **defaults)


def _reclaim_active(**overrides):
    """回収（ゾンビ／タイムアウト）対象のActiveWorktree。

    worktreeディレクトリが存在せず、pidも記録されていない状態を既定にする。
    """
    defaults = {
        "worktree_path": "worktrees/missing-280",
        "pid": None,
        "started_at": 1_000.0,
    }
    defaults.update(overrides)
    return _active(**defaults)


def _task(**overrides):
    """GC系テストの既定Task（Issue 280・`status:not-needed`）。"""
    defaults = {
        "footprint": ("src/foo.py",),
        "status_labels": ("status:not-needed",),
    }
    defaults.update(overrides)
    return make_test_task(defaults.pop("issue_number", 280), **defaults)


def _in_progress_task(**overrides):
    """回収系テストが使う`status:in-progress`なTask。"""
    defaults = {"status_labels": ("status:in-progress",)}
    defaults.update(overrides)
    return _task(**defaults)
