"""Issue #799: `depends_on`解決の共通resolverが、subtask_idのグローバル一意性を
前提にせず、親Issue単位でスコープした解決を行うことを検証する。"""

from __future__ import annotations

from orchestune.dispatch.dependency_resolution import (
    REASON_AMBIGUOUS,
    REASON_MISSING,
    REASON_UNKNOWN_PARENT,
    TaskDependencies,
    resolve_all_dependencies,
    resolve_task_dependencies,
)
from orchestune.models import Task


def _task(
    issue_number: int,
    subtask_id: str,
    *,
    parent_number: int | None,
    depends_on: tuple[str, ...] = (),
    native_depends_on: tuple[int, ...] = (),
) -> Task:
    return Task(
        issue_number=issue_number,
        subtask_id=subtask_id,
        footprint=(),
        symbols=(),
        risk=False,
        priority="medium",
        progress_partial=False,
        status_labels=(),
        created_at="2026-01-01T00:00:00Z",
        depends_on=depends_on,
        native_depends_on=native_depends_on,
        parent_number=parent_number,
    )


def test_same_subtask_id_in_different_epics_does_not_collide() -> None:
    """別EPIC（別parent）が同名subtask_idを使っても取り違えない。"""
    upstream_a = _task(101, "backend-api", parent_number=100)
    downstream_a = _task(
        102, "frontend", parent_number=100, depends_on=("backend-api",)
    )
    upstream_b = _task(201, "backend-api", parent_number=200)
    downstream_b = _task(
        202, "frontend", parent_number=200, depends_on=("backend-api",)
    )
    tasks_by_issue = {
        t.issue_number: t for t in (upstream_a, downstream_a, upstream_b, downstream_b)
    }

    result = resolve_all_dependencies(tasks_by_issue)

    assert result[102].resolved == (101,)
    assert result[102].unresolved == ()
    assert result[202].resolved == (201,)
    assert result[202].unresolved == ()


def test_duplicate_subtask_id_within_same_parent_is_ambiguous() -> None:
    """同一親配下に同名subtask_idが重複（計画再作成の残骸）した場合は未解決。"""
    old_upstream = _task(101, "setup", parent_number=100)
    new_upstream = _task(103, "setup", parent_number=100)
    downstream = _task(102, "build", parent_number=100, depends_on=("setup",))
    tasks_by_issue = {
        t.issue_number: t for t in (old_upstream, new_upstream, downstream)
    }

    result = resolve_all_dependencies(tasks_by_issue)

    assert result[102].resolved == ()
    assert len(result[102].unresolved) == 1
    unresolved = result[102].unresolved[0]
    assert unresolved.raw == "setup"
    assert unresolved.reason == REASON_AMBIGUOUS
    assert unresolved.candidates == (101, 103)


def test_ambiguous_resolution_is_order_independent() -> None:
    """候補の入力順序を反転しても解決結果は変わらない（後勝ち・先勝ちにしない）。"""
    old_upstream = _task(101, "setup", parent_number=100)
    new_upstream = _task(103, "setup", parent_number=100)
    downstream = _task(102, "build", parent_number=100, depends_on=("setup",))

    forward = {t.issue_number: t for t in (old_upstream, new_upstream, downstream)}
    reversed_order = {
        t.issue_number: t for t in (downstream, new_upstream, old_upstream)
    }

    forward_result = resolve_task_dependencies(downstream, forward)
    reversed_result = resolve_task_dependencies(downstream, reversed_order)

    assert forward_result == reversed_result
    assert forward_result.unresolved[0].reason == REASON_AMBIGUOUS


def test_unknown_parent_leaves_body_dependency_unresolved() -> None:
    """自タスクのparentが不明な場合、本文依存は共通スコープへ格上げされず未解決。"""
    upstream = _task(101, "setup", parent_number=100)
    downstream = _task(102, "build", parent_number=None, depends_on=("setup",))
    tasks_by_issue = {t.issue_number: t for t in (upstream, downstream)}

    result = resolve_task_dependencies(downstream, tasks_by_issue)

    assert result.resolved == ()
    assert result.unresolved == (
        result.unresolved[0].__class__(raw="setup", reason=REASON_UNKNOWN_PARENT),
    )


def test_unknown_parent_is_not_resolved_even_if_name_is_globally_unique() -> None:
    """全体でその名前が1件しかなくても、親不明を理由に解決してはならない。"""
    upstream = _task(101, "only-one-named-this", parent_number=100)
    downstream = _task(
        102, "build", parent_number=None, depends_on=("only-one-named-this",)
    )
    tasks_by_issue = {t.issue_number: t for t in (upstream, downstream)}

    result = resolve_task_dependencies(downstream, tasks_by_issue)

    assert result.resolved == ()
    assert result.unresolved[0].reason == REASON_UNKNOWN_PARENT


def test_missing_dependency_target_is_unresolved() -> None:
    downstream = _task(102, "build", parent_number=100, depends_on=("nope",))
    tasks_by_issue = {102: downstream}

    result = resolve_task_dependencies(downstream, tasks_by_issue)

    assert result.resolved == ()
    assert result.unresolved[0].reason == REASON_MISSING
    assert result.unresolved[0].candidates == ()


def test_native_dependency_across_epics_is_resolved_by_number() -> None:
    """ネイティブblocked_byはIssue番号のまま扱われ、別EPICへの依存も解決できる。"""
    other_epic_upstream = _task(301, "unrelated-name", parent_number=300)
    downstream = _task(102, "build", parent_number=100, native_depends_on=(301,))
    tasks_by_issue = {t.issue_number: t for t in (other_epic_upstream, downstream)}

    result = resolve_task_dependencies(downstream, tasks_by_issue)

    assert result.resolved == (301,)
    assert result.unresolved == ()


def test_native_dependency_outside_candidate_set_stays_unresolved() -> None:
    """候補集合（今サイクルで取得したIssue）に無いネイティブ依存は消えず未解決。"""
    downstream = _task(102, "build", parent_number=100, native_depends_on=(999,))
    tasks_by_issue = {102: downstream}

    result = resolve_task_dependencies(downstream, tasks_by_issue)

    assert result.resolved == ()
    assert result.unresolved[0].reason == REASON_MISSING
    assert result.unresolved[0].candidates == (999,)


def test_partial_native_link_does_not_drop_body_only_dependency() -> None:
    """ネイティブリンクが一部しか無い場合も、本文だけの依存を落とさない。"""
    native_dep = _task(301, "native-linked", parent_number=100)
    body_only_dep = _task(302, "body-linked", parent_number=100)
    downstream = _task(
        102,
        "build",
        parent_number=100,
        depends_on=("body-linked",),
        native_depends_on=(301,),
    )
    tasks_by_issue = {
        t.issue_number: t for t in (native_dep, body_only_dep, downstream)
    }

    result = resolve_task_dependencies(downstream, tasks_by_issue)

    assert set(result.resolved) == {301, 302}
    assert result.unresolved == ()


def test_native_and_body_dependency_to_same_issue_deduplicate_by_number() -> None:
    upstream = _task(301, "setup", parent_number=100)
    downstream = _task(
        102,
        "build",
        parent_number=100,
        depends_on=("setup",),
        native_depends_on=(301,),
    )
    tasks_by_issue = {t.issue_number: t for t in (upstream, downstream)}

    result = resolve_task_dependencies(downstream, tasks_by_issue)

    assert result.resolved == (301,)


def test_ambiguous_body_dependency_is_not_cleared_by_same_named_native_dependency() -> (
    None
):
    """同名のネイティブ依存があるだけで、曖昧な本文依存を消してはならない。"""
    old_upstream = _task(301, "setup", parent_number=100)
    new_upstream = _task(303, "setup", parent_number=100)
    other_native_dep = _task(401, "other", parent_number=100)
    downstream = _task(
        102,
        "build",
        parent_number=100,
        depends_on=("setup",),
        native_depends_on=(401,),
    )
    tasks_by_issue = {
        t.issue_number: t
        for t in (old_upstream, new_upstream, other_native_dep, downstream)
    }

    result = resolve_task_dependencies(downstream, tasks_by_issue)

    assert result.resolved == (401,)
    assert len(result.unresolved) == 1
    assert result.unresolved[0].raw == "setup"
    assert result.unresolved[0].reason == REASON_AMBIGUOUS
    assert result.unresolved[0].candidates == (301, 303)


def test_same_issue_fetched_via_multiple_status_paths_deduplicates() -> None:
    """同じIssueが複数ステータス経路で取得されても重複解決にならない。"""
    upstream = _task(301, "setup", parent_number=100)
    downstream = _task(
        102,
        "build",
        parent_number=100,
        depends_on=("setup",),
        native_depends_on=(301,),
    )
    # tasks_by_issue is keyed by issue number, so duplicated fetches of #301
    # (e.g. appearing under both `blocked` and `done`) collapse to one entry —
    # this test documents that guarantee at the resolver's boundary.
    tasks_by_issue = {t.issue_number: t for t in (upstream, downstream)}

    result = resolve_task_dependencies(downstream, tasks_by_issue)

    assert result.resolved == (301,)
    assert result.unresolved == ()


def test_no_dependencies_resolves_empty() -> None:
    task = _task(102, "build", parent_number=100)
    result = resolve_task_dependencies(task, {102: task})
    assert result == TaskDependencies()
    assert result.is_fully_resolved


def test_normal_same_epic_resolution_has_no_regression() -> None:
    """通常の同一EPIC内の依存解決（回帰なし）。"""
    a = _task(1, "a", parent_number=100)
    b = _task(2, "b", parent_number=100, depends_on=("a",))
    c = _task(3, "c", parent_number=100, depends_on=("a", "b"))
    tasks_by_issue = {t.issue_number: t for t in (a, b, c)}

    result = resolve_all_dependencies(tasks_by_issue)

    assert result[1] == TaskDependencies()
    assert result[2].resolved == (1,)
    assert set(result[3].resolved) == {1, 2}
    assert all(not deps.unresolved for deps in result.values())
