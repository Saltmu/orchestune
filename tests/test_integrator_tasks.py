"""#291: `mock.patch`によるグローバルなクラスメソッド差し替えではなく、
`forge`引数への注入だけでテストが書けることを示す。"""

from __future__ import annotations

import re
from unittest.mock import MagicMock, patch

from orchestune.integrator_tasks import get_sorted_done_tasks
from orchestune.models import IssueRecord


def _done_issue(
    number: int, subtask_id: str, *, parent: dict | None = None, parent_in_body=None
) -> IssueRecord:
    body_lines = ["```yaml", f"subtask_id: {subtask_id}", "footprint: []"]
    if parent_in_body is not None:
        body_lines.append(f"parent_issue_number: {parent_in_body}")
    body_lines.append("```\n")
    return IssueRecord(
        number=number,
        title=f"Issue {number}",
        body="\n".join(body_lines),
        labels=("status:done",),
        created_at="2026-07-13T00:00:00Z",
        parent=parent,
    )


def test_returns_empty_when_no_done_issues_using_injected_fake_forge():
    fake_forge = MagicMock()
    fake_forge.list_issues_by_label.return_value = []

    sorted_done, unparsable = get_sorted_done_tasks(None, forge=fake_forge)

    assert sorted_done == []
    assert unparsable == []
    fake_forge.list_issues_by_label.assert_called_once_with("status:done", state="all")


def test_sorts_done_tasks_using_injected_fake_forge():
    done_issue = _done_issue(1, "task-1")
    fake_forge = MagicMock()
    fake_forge.list_issues_by_label.side_effect = lambda label, state="open": (
        [done_issue] if label == "status:done" else []
    )

    sorted_done, unparsable = get_sorted_done_tasks(None, forge=fake_forge)

    assert unparsable == []
    assert [task.subtask_id for task in sorted_done] == ["task-1"]


def test_parent_scoped_filter_includes_metadata_only_children():
    """#485 review (P1): a child created while native sub-issue linking was
    unavailable has no `IssueRecord.parent`, only the body's
    `parent_issue_number`. It must still be scoped into
    `get_sorted_done_tasks(parent_issue_number=...)`, or the integrator
    never merges/closes it and `process_parent_completion` waits forever."""
    metadata_only_done = _done_issue(1, "task-1", parent=None, parent_in_body=100)
    fake_forge = MagicMock()
    fake_forge.list_issues_by_label.side_effect = lambda label, state="open": (
        [metadata_only_done] if label == "status:done" else []
    )

    sorted_done, unparsable = get_sorted_done_tasks(100, forge=fake_forge)

    assert unparsable == []
    assert [task.subtask_id for task in sorted_done] == ["task-1"]


def test_parent_scoped_filter_prefers_native_parent_over_stale_body_metadata():
    """#485 review (P2): once natively reparented, `issue.parent` is
    authoritative even if the body's `parent_issue_number` is stale (never
    rewritten) — otherwise the issue could indefinitely block the old
    parent's completion under the wrong scope."""
    reparented_done = _done_issue(
        1, "task-1", parent={"number": 200}, parent_in_body=100
    )
    fake_forge = MagicMock()
    fake_forge.list_issues_by_label.side_effect = lambda label, state="open": (
        [reparented_done] if label == "status:done" else []
    )

    sorted_done, _ = get_sorted_done_tasks(100, forge=fake_forge)
    assert sorted_done == []

    sorted_done, _ = get_sorted_done_tasks(200, forge=fake_forge)
    assert [task.subtask_id for task in sorted_done] == ["task-1"]


def test_ignore_patterns_default_to_empty_tuple_when_unspecified():
    """#407: `ignore_patterns`省略時は`build_dag`が空タプルで呼ばれる
    （既定の挙動を変えない）。"""
    done_issue = _done_issue(1, "task-1")
    fake_forge = MagicMock()
    fake_forge.list_issues_by_label.side_effect = lambda label, state="open": (
        [done_issue] if label == "status:done" else []
    )

    with patch(
        "orchestune.integrator_tasks.build_dag", wraps=lambda *a, **kw: MagicMock()
    ) as mock_build_dag:
        get_sorted_done_tasks(None, forge=fake_forge)

    mock_build_dag.assert_called_once()
    assert mock_build_dag.call_args.kwargs.get("ignore_patterns", ()) == ()


def test_ignore_patterns_are_forwarded_to_build_dag():
    """#407: `orchestune-dag`向けの`dag_ignore_patterns`と同じく、
    `get_sorted_done_tasks`に渡された`ignore_patterns`が`build_dag`へ
    そのまま伝搬されること。無視されるべきfootprint衝突が明示的な依存関係と
    組み合わさってDagCycleErrorを誘発する（#404と同様の）回帰を防ぐ。"""
    done_issue = _done_issue(1, "task-1")
    fake_forge = MagicMock()
    fake_forge.list_issues_by_label.side_effect = lambda label, state="open": (
        [done_issue] if label == "status:done" else []
    )
    patterns = (re.compile(r"(^|/)package\.json$"),)

    with patch(
        "orchestune.integrator_tasks.build_dag", wraps=lambda *a, **kw: MagicMock()
    ) as mock_build_dag:
        get_sorted_done_tasks(None, forge=fake_forge, ignore_patterns=patterns)

    mock_build_dag.assert_called_once()
    assert mock_build_dag.call_args.kwargs.get("ignore_patterns") == patterns


def test_threshold_defaults_to_default_similarity_threshold_when_unspecified():
    """#407/#415レビュー指摘: `threshold`省略時は
    `DEFAULT_SIMILARITY_THRESHOLD`が`build_dag`へ渡る（既定挙動を変えない）。"""
    from orchestune.dag_similarity import DEFAULT_SIMILARITY_THRESHOLD

    done_issue = _done_issue(1, "task-1")
    fake_forge = MagicMock()
    fake_forge.list_issues_by_label.side_effect = lambda label, state="open": (
        [done_issue] if label == "status:done" else []
    )

    with patch(
        "orchestune.integrator_tasks.build_dag", wraps=lambda *a, **kw: MagicMock()
    ) as mock_build_dag:
        get_sorted_done_tasks(None, forge=fake_forge)

    mock_build_dag.assert_called_once()
    assert (
        mock_build_dag.call_args.kwargs.get("threshold") == DEFAULT_SIMILARITY_THRESHOLD
    )


def test_threshold_is_forwarded_to_build_dag():
    """#407/#415レビュー指摘: `orchestune-dag --threshold`向けの
    `dag_similarity_threshold`と同じく、`get_sorted_done_tasks`に渡された
    `threshold`が`build_dag`へそのまま伝搬されること。伝搬しないと、
    低い閾値で意図的に作った類似度エッジがintegrator側の既定閾値再計算で
    消え、統合順序（topological_order）が食い違いうる。"""
    done_issue = _done_issue(1, "task-1")
    fake_forge = MagicMock()
    fake_forge.list_issues_by_label.side_effect = lambda label, state="open": (
        [done_issue] if label == "status:done" else []
    )

    with patch(
        "orchestune.integrator_tasks.build_dag", wraps=lambda *a, **kw: MagicMock()
    ) as mock_build_dag:
        get_sorted_done_tasks(None, forge=fake_forge, threshold=0.1)

    mock_build_dag.assert_called_once()
    assert mock_build_dag.call_args.kwargs.get("threshold") == 0.1
