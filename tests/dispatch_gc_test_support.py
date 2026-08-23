"""tests/test_dispatch_gc_*.py群が共有するヘルパー・フィクスチャデータ。

test_dispatch_gc.py (1418行) を、ルール別・クリーンアップ別
(#479: git primitives / stale entry rules / completed rule / integration)
へ分割した際、各ファイルから共通利用される`_ctx`/`_active`/`_task`/`_issue`
をこのモジュールへ切り出した。`test_`で始まらないためpytestには収集されない。
"""

import tempfile
from pathlib import Path
from typing import Any

from orchestune.dispatch_config import DispatcherConfig
from orchestune.dispatch_rules import CycleContext
from orchestune.dispatch_scoring import Task
from orchestune.dispatch_state import ActiveWorktree, RunState
from tests.conftest import make_issue

tmp_path = Path(tempfile.mkdtemp(prefix="orchestune-test-state-"))


def _ctx(*, forge: Any | None = None, **overrides):
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
