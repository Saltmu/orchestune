"""tests/test_dispatch_gc_*.py群が共有するヘルパー・フィクスチャデータ。

test_dispatch_gc.py (1418行) を、ルール別・クリーンアップ別
(#479: git primitives / stale entry rules / completed rule / integration)
へ分割した際、各ファイルから共通利用される`_ctx`/`_active`/`_task`/`_issue`
をこのモジュールへ切り出した。`test_`で始まらないためpytestには収集されない。
"""

import tempfile
from collections.abc import Sequence
from pathlib import Path

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
from orchestune.dispatch.rules import CycleContext
from orchestune.dispatch.scoring import Task
from orchestune.dispatch.state import ActiveWorktree, RunState
from orchestune.models import PrRecord
from tests.conftest import make_issue

tmp_path = Path(tempfile.mkdtemp(prefix="orchestune-test-state-"))


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
    defaults = dict(
        run_state=RunState(active_worktrees={}),
        tasks_by_issue={},
        issue_number_by_subtask_id={},
        done_subtask_ids=set(),
        ci_passed_pr_subtask_ids=set(),
        changes_requested_subtask_ids=set(),
        subtask_branch_map={},
        prs=[],
        pr_by_branch={},
        config=DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            forge=forge,
        ),
    )
    defaults.update(overrides)
    return CycleContext(**defaults)


def _active(**overrides):
    defaults = dict(
        issue_number=280,
        branch="claude/issue-280-task-a",
        worktree_path="worktrees/w1",
        pid=111,
        started_at=1_699_999_000.0,
        declared_footprint=("src/foo.py",),
    )
    defaults.update(overrides)
    return ActiveWorktree(**defaults)


def _task(**overrides):
    defaults = dict(
        issue_number=280,
        subtask_id="task-a",
        footprint=("src/foo.py",),
        symbols=(),
        risk=False,
        priority="medium",
        progress_partial=False,
        status_labels=("status:not-needed",),
        created_at="2026-01-01T00:00:00+00:00",
    )
    defaults.update(overrides)
    return Task(**defaults)


def _issue(
    number,
    labels=("status:queued",),
    footprint=("src/foo.py",),
    symbols=("foo.Foo",),
    subtask_id="task-a",
    depends_on=(),
    created_at="2026-01-01T00:00:00+00:00",
    parent_number=181,
):
    """`tests/conftest.py`の`make_issue`に、このファイルの旧テスト群が前提と
    する`parent_number`（既定181）とtitleを合わせた薄いラッパー。"""
    parent = {"number": parent_number} if parent_number is not None else None
    return make_issue(
        number,
        title="t",
        labels=labels,
        footprint=footprint,
        symbols=symbols,
        subtask_id=subtask_id,
        depends_on=depends_on,
        created_at=created_at,
        parent=parent,
    )
