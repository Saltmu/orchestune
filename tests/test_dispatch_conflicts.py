"""`orchestune.dispatch.conflicts.subtasks_from_tasks`のテスト。"""

from __future__ import annotations

from orchestune.dispatch.conflicts import subtasks_from_tasks
from orchestune.models import Task


def _task(issue_number, subtask_id, **overrides):
    defaults = dict(
        issue_number=issue_number,
        subtask_id=subtask_id,
        footprint=(),
        symbols=(),
        risk=False,
        priority="medium",
        progress_partial=False,
        status_labels=("status:queued",),
        created_at="2026-01-01T00:00:00+00:00",
    )
    defaults.update(overrides)
    return Task(**defaults)


def test_body_depends_on_is_preserved():
    upstream = _task(1, "a")
    downstream = _task(2, "b", depends_on=("a",))

    subtasks = subtasks_from_tasks([upstream, downstream])

    assert subtasks["b"].depends_on == ("a",)


def test_native_only_dependency_is_not_dropped():
    """#799レビュー指摘(Codex P2): `Task.depends_on`が本文専用になったため、
    ネイティブ`blocked_by`しか宣言していない依存をfootprint DAG/
    conflict graphから落としてはならない（誤って
    `status:blocked-recompute`を検知したり、依存元との正当な重複を
    競合と誤判定したりしうる）。
    """
    upstream = _task(1, "a")
    downstream = _task(2, "b", native_depends_on=(1,))

    subtasks = subtasks_from_tasks([upstream, downstream])

    assert subtasks["b"].depends_on == ("a",)


def test_native_and_body_dependencies_are_merged_without_duplication():
    upstream_a = _task(1, "a")
    upstream_b = _task(3, "c")
    downstream = _task(2, "b", depends_on=("c",), native_depends_on=(1,))

    subtasks = subtasks_from_tasks([upstream_a, upstream_b, downstream])

    assert subtasks["b"].depends_on == ("a", "c")


def test_native_dependency_outside_population_is_ignored():
    """候補集合内で解決できないネイティブ依存は辺を作れないため無視する
    （`legacy_merged_depends_on`は候補集合外のIssue番号を素通しする）。"""
    downstream = _task(2, "b", native_depends_on=(999,))

    subtasks = subtasks_from_tasks([downstream])

    assert subtasks["b"].depends_on == ()
