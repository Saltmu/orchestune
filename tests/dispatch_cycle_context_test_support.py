"""`CycleContext`のAPI契約テスト群が共有する準備処理（#916）。

`tests/test_dispatch_cycle_context_api.py` / `..._context_records.py` /
`..._port_contracts.py` は #868・#881 で「semantic query / record / port契約」
という別々の観点へ分かれたが、入力の作り方（Issue番号から一意に導く
`subtask_id`・`branch`・`pid`、`StatusLabel`既定、Z表記の`created_at`）は
共通である。その共通部分だけをここへ集約し、観点は各ファイルに残す。

素の既定値は`tests/dispatch_test_support.py`が持ち、このモジュールは
「Context契約系の既定値」への薄い上書きに徹する。`test_`で始まらないため
pytestには収集されない。
"""

from __future__ import annotations

from typing import Any

from orchestune.dispatch.rules import CycleContext
from orchestune.dispatch.state import ActiveWorktree
from orchestune.labels import StatusLabel
from orchestune.models import Task
from tests.dispatch_test_support import (
    make_state_root,
    make_test_active_worktree,
    make_test_cycle_context,
    make_test_task,
)

#: Context契約系テストの`created_at`。`dispatch_test_support`の既定
#: （`+00:00`表記）とは表記だけが異なる。#868当時の入力をそのまま保つ。
CONTEXT_CREATED_AT = "2026-01-01T00:00:00Z"

_TMP = make_state_root(prefix="orchestune-test-cycle-context-")


def _task(issue_number: int, **overrides: Any) -> Task:
    """Issue番号から`subtask_id`を導く`status:queued`なTask。"""
    defaults: dict[str, Any] = {
        "subtask_id": f"task-{issue_number}",
        "status_labels": (StatusLabel.QUEUED,),
        "created_at": CONTEXT_CREATED_AT,
        "issue_state": "OPEN",
    }
    defaults.update(overrides)
    return make_test_task(issue_number, **defaults)


def _active(issue_number: int, **overrides: Any) -> ActiveWorktree:
    """Issue番号から`branch`/`worktree_path`/`pid`を導くActiveWorktree。

    同一contextへ複数のworktreeを積むテストが多いため、識別子をIssue番号から
    一意に導き、どのエントリが誰のものか読んで分かるようにしている。
    """
    defaults: dict[str, Any] = {
        "branch": f"claude/issue-{issue_number}-task",
        "worktree_path": f"worktrees/w{issue_number}",
        "pid": 1000 + issue_number,
        "started_at": 1_700_000_000.0,
    }
    defaults.update(overrides)
    return make_test_active_worktree(issue_number, **defaults)


def _ctx(**overrides: Any) -> CycleContext:
    """action portを束ねない、観測だけのCycleContext。"""
    return make_test_cycle_context(state_root=_TMP, **overrides)
