"""dispatch_reconciliation.py の復元・整合性修復に関する境界値テスト (#337)。

`_collect_active_conflict_subtask_ids` / `_handle_blocked_recompute_recovery`
は既存の `tests/test_dispatch_cycle.py`
では実質未検証だったため、本ファイルで単体テストとして完結させる。
"""

import tempfile
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from orchestune.consistency.models import ConsistencyScope, ObservedRepositoryState
from orchestune.consistency.vocabulary import DESIRED_DEPENDENCIES_RESOLVED
from orchestune.dag.models import (
    FootprintConflict,
    SubTask,
    compile_extra_ignore_patterns,
)
from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.cycle import _DispatchConsistencyAdapter
from orchestune.dispatch.dependency_resolution import resolve_all_dependencies
from orchestune.dispatch.reconciliation import (
    _collect_active_conflict_subtask_ids,
    _handle_blocked_recompute_recovery,
)
from orchestune.dispatch.rules import CycleContext
from orchestune.dispatch.scoring import Task
from orchestune.dispatch.state import ActiveWorktree, RunState
from orchestune.models import IssueRecord

tmp_path = Path(tempfile.mkdtemp(prefix="orchestune-test-reconciliation-"))


def _task(**overrides):
    defaults = dict(
        issue_number=1,
        subtask_id="task-a",
        footprint=(),
        symbols=(),
        risk=False,
        priority="medium",
        progress_partial=False,
        status_labels=("status:in-progress",),
        created_at="2026-01-01T00:00:00+00:00",
        depends_on=(),
        parent_number=100,
    )
    defaults.update(overrides)
    return Task(**defaults)


def _dependency_task(issue_number=2, subtask_id="task-x", **overrides):
    """`depends_on=("task-x",)`が解決される依存先タスク。"""
    return _task(
        issue_number=issue_number,
        subtask_id=subtask_id,
        status_labels=(),
        **overrides,
    )


def _active(**overrides):
    defaults = dict(
        issue_number=1,
        branch="claude/issue-1-task-a",
        worktree_path="worktrees/w1",
        pid=111,
        started_at=1_699_999_000.0,
        declared_footprint=(),
    )
    defaults.update(overrides)
    return ActiveWorktree(**defaults)


def _ctx(**overrides):
    defaults = dict(
        run_state=RunState(active_worktrees={}),
        tasks_by_issue={},
        dependency_resolution={},
        ci_passed_pr_issue_numbers=set(),
        changes_requested_issue_numbers=set(),
        branch_by_issue_number={},
        prs=[],
        config=DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
        ),
    )
    defaults.update(overrides)
    if "dependency_resolution" not in overrides and "tasks_by_issue" in overrides:
        defaults["dependency_resolution"] = resolve_all_dependencies(
            overrides["tasks_by_issue"]
        )
    return CycleContext(**defaults)


def _issue(number, labels=(), state="OPEN"):
    return IssueRecord(
        number=number,
        title=f"Issue {number}",
        body="",
        labels=labels,
        created_at="2026-01-01T00:00:00+00:00",
        state=state,
    )


class _IssuesStub:
    """`_handle_blocked_recompute_recovery`が要求する`.all()`のみを持つ最小スタブ。"""

    def __init__(self, issues):
        self._issues = list(issues)

    def all(self):
        return list(self._issues)


class TestCollectActiveConflictSubtaskIds:
    def test_skips_active_worktree_without_matching_task(self, tmp_path):
        active = _active(issue_number=99)
        run_state = RunState(active_worktrees={"w1": active})
        ctx = _ctx(tasks_by_issue={})
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
        )

        with patch(
            "orchestune.dispatch.reconciliation.check_footprint_deviation",
            autospec=True,
            side_effect=AssertionError("タスク未対応のactiveは検査すべきではない"),
        ):
            result = _collect_active_conflict_subtask_ids(run_state, ctx, {}, config)

        assert result == set()

    def test_skips_active_worktree_when_task_has_no_subtask_id(self, tmp_path):
        task = _task(issue_number=1, subtask_id="")
        active = _active(issue_number=1)
        run_state = RunState(active_worktrees={"w1": active})
        ctx = _ctx(tasks_by_issue={1: task})
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
        )

        with patch(
            "orchestune.dispatch.reconciliation.check_footprint_deviation",
            autospec=True,
            side_effect=AssertionError("subtask_id未設定のactiveは検査すべきではない"),
        ):
            result = _collect_active_conflict_subtask_ids(run_state, ctx, {}, config)

        assert result == set()

    def test_fail_closed_when_deviation_undetectable(self, tmp_path):
        """deviatedがNone（検出不能エラー）の場合は全サブタスクを競合中として扱う。"""
        task = _task(issue_number=1, subtask_id="task-a")
        active = _active(issue_number=1)
        run_state = RunState(active_worktrees={"w1": active})
        ctx = _ctx(tasks_by_issue={1: task})
        subtasks_for_recompute = {"task-a": object(), "task-b": object()}
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
        )

        with patch(
            "orchestune.dispatch.reconciliation.check_footprint_deviation",
            autospec=True,
            return_value=None,
        ):
            result = _collect_active_conflict_subtask_ids(
                run_state, ctx, subtasks_for_recompute, config
            )

        assert result == {"task-a", "task-b"}

    def test_adds_blocked_subtask_ids_from_recomputed_conflicts(self, tmp_path):
        task = _task(issue_number=1, subtask_id="task-a", footprint=("a.py",))
        active = _active(
            issue_number=1,
            declared_footprint=("a.py",),
            worktree_path="worktrees/w1",
        )
        run_state = RunState(active_worktrees={"w1": active})
        ctx = _ctx(tasks_by_issue={1: task})
        subtasks_for_recompute = {"task-a": object(), "task-b": object()}
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
        )
        conflicts = [
            FootprintConflict(
                subtask_id="task-a",
                other_subtask_id="task-b",
                similarity=0.9,
                blocked_subtask_id="task-b",
            )
        ]

        with (
            patch(
                "orchestune.dispatch.reconciliation.check_footprint_deviation",
                autospec=True,
                return_value=["b.py"],
            ),
            patch(
                "orchestune.dispatch.reconciliation.recompute_dag_for_footprint_change",
                autospec=True,
                return_value=(MagicMock(), conflicts),
            ) as mock_recompute,
        ):
            result = _collect_active_conflict_subtask_ids(
                run_state, ctx, subtasks_for_recompute, config
            )

        assert result == {"task-b"}
        mock_recompute.assert_called_once_with(
            subtasks_for_recompute,
            "task-a",
            updated_footprint=("a.py", "b.py"),
            threshold=config.dag_similarity_threshold,
            ignore_patterns=config.dag_ignore_patterns,
        )

    def test_conflict_without_blocked_subtask_id_is_ignored(self, tmp_path):
        task = _task(issue_number=1, subtask_id="task-a")
        active = _active(issue_number=1)
        run_state = RunState(active_worktrees={"w1": active})
        ctx = _ctx(tasks_by_issue={1: task})
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
        )
        conflicts = [
            FootprintConflict(
                subtask_id="task-a",
                other_subtask_id="task-c",
                similarity=0.9,
                blocked_subtask_id="",
            )
        ]

        with (
            patch(
                "orchestune.dispatch.reconciliation.check_footprint_deviation",
                autospec=True,
                return_value=["c.py"],
            ),
            patch(
                "orchestune.dispatch.reconciliation.recompute_dag_for_footprint_change",
                autospec=True,
                return_value=(MagicMock(), conflicts),
            ),
        ):
            result = _collect_active_conflict_subtask_ids(run_state, ctx, {}, config)

        assert result == set()

    def test_fail_closed_when_recompute_raises(self, tmp_path):
        task = _task(issue_number=1, subtask_id="task-a")
        active = _active(issue_number=1)
        run_state = RunState(active_worktrees={"w1": active})
        ctx = _ctx(tasks_by_issue={1: task})
        subtasks_for_recompute = {"task-a": object(), "task-b": object()}
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
        )

        with (
            patch(
                "orchestune.dispatch.reconciliation.check_footprint_deviation",
                autospec=True,
                return_value=["b.py"],
            ),
            patch(
                "orchestune.dispatch.reconciliation.recompute_dag_for_footprint_change",
                autospec=True,
                side_effect=RuntimeError("DAG再計算エラー"),
            ),
        ):
            result = _collect_active_conflict_subtask_ids(
                run_state, ctx, subtasks_for_recompute, config
            )

        assert result == {"task-a", "task-b"}

    def _subtask(self, id_, footprint):
        return SubTask(
            id=id_,
            description="",
            footprint=footprint,
            symbols=(),
            depends_on=(),
            risk=False,
            risk_reasons=(),
        )

    def test_dag_ignore_patterns_suppresses_recompute_conflict(self, tmp_path):
        """#398/#404: orchestune-dag向けのdag_ignore_patternsは、
        dispatcherの実行時DAG再計算（reconciliation側）にも適用され、
        初回検証で無視される設定のファイルの衝突を誤って競合検知しないこと。"""
        task = _task(issue_number=1, subtask_id="task-a", footprint=("src/only_a.py",))
        active = _active(issue_number=1, declared_footprint=("src/only_a.py",))
        run_state = RunState(active_worktrees={"w1": active})
        ctx = _ctx(tasks_by_issue={1: task})
        subtasks_for_recompute = {
            "task-a": self._subtask("task-a", ("src/only_a.py",)),
            "task-b": self._subtask("task-b", ("package.json",)),
        }

        config_without_ignore = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
        )
        with patch(
            "orchestune.dispatch.reconciliation.check_footprint_deviation",
            autospec=True,
            return_value=["package.json"],
        ):
            result_without_ignore = _collect_active_conflict_subtask_ids(
                run_state, ctx, subtasks_for_recompute, config_without_ignore
            )
        assert result_without_ignore == {"task-b"}

        config_with_ignore = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            dag_ignore_patterns=compile_extra_ignore_patterns([r"(^|/)package\.json$"]),
        )
        with patch(
            "orchestune.dispatch.reconciliation.check_footprint_deviation",
            autospec=True,
            return_value=["package.json"],
        ):
            result_with_ignore = _collect_active_conflict_subtask_ids(
                run_state, ctx, subtasks_for_recompute, config_with_ignore
            )
        assert result_with_ignore == set()

    def test_dag_similarity_threshold_is_forwarded_to_recompute(self, tmp_path):
        """#407/#415レビュー指摘: dag_similarity_thresholdもdag_ignore_patterns
        と同様にreconciliation側の実行時DAG再計算へ伝搬させること。"""
        task = _task(issue_number=1, subtask_id="task-a", footprint=("src/only_a.py",))
        active = _active(issue_number=1, declared_footprint=("src/only_a.py",))
        run_state = RunState(active_worktrees={"w1": active})
        ctx = _ctx(tasks_by_issue={1: task})
        subtasks_for_recompute = {"task-a": object(), "task-b": object()}
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            dag_similarity_threshold=0.1,
        )

        with (
            patch(
                "orchestune.dispatch.reconciliation.check_footprint_deviation",
                autospec=True,
                return_value=["package.json"],
            ),
            patch(
                "orchestune.dispatch.reconciliation.recompute_dag_for_footprint_change",
                autospec=True,
                return_value=(MagicMock(), []),
            ) as mock_recompute,
        ):
            _collect_active_conflict_subtask_ids(
                run_state, ctx, subtasks_for_recompute, config
            )

        assert mock_recompute.call_args.kwargs.get("threshold") == 0.1


class TestHandleBlockedRecomputeRecovery:
    def test_returns_empty_when_no_blocked_recompute_issues(self, tmp_path):
        run_state = RunState(active_worktrees={})
        ctx = _ctx()
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
        )

        result = _handle_blocked_recompute_recovery(
            _IssuesStub([_issue(1, labels=("status:queued",))]),
            run_state,
            ctx,
            config,
        )

        assert result == []

    def test_issue_without_matching_task_is_skipped(self, tmp_path):
        run_state = RunState(active_worktrees={})
        ctx = _ctx(tasks_by_issue={})
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            apply=True,
        )

        with (
            patch("fake_forge_proxy.active_fake_forge.remove_label") as mock_remove,
            patch("fake_forge_proxy.active_fake_forge.add_label") as mock_add,
        ):
            result = _handle_blocked_recompute_recovery(
                _IssuesStub(
                    [_issue(1, labels=("status:blocked-recompute", "status:blocked"))]
                ),
                run_state,
                ctx,
                config,
            )

        mock_remove.assert_not_called()
        mock_add.assert_not_called()
        assert result == []

    def test_dry_run_returns_event_without_calling_github(self, tmp_path):
        task = _task(
            issue_number=1, subtask_id="task-a", depends_on=(), status_labels=()
        )
        run_state = RunState(active_worktrees={})
        ctx = _ctx(tasks_by_issue={1: task})
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            apply=False,
        )

        with (
            patch("fake_forge_proxy.active_fake_forge.remove_label") as mock_remove,
            patch("fake_forge_proxy.active_fake_forge.add_label") as mock_add,
        ):
            result = _handle_blocked_recompute_recovery(
                _IssuesStub(
                    [_issue(1, labels=("status:blocked-recompute", "status:blocked"))]
                ),
                run_state,
                ctx,
                config,
            )

        mock_remove.assert_not_called()
        mock_add.assert_not_called()
        assert result == [{"issue_number": 1, "subtask_id": "task-a"}]

    def test_apply_promotes_when_dependencies_are_resolved(self, tmp_path):
        task = _task(
            issue_number=1,
            subtask_id="task-a",
            depends_on=("task-x",),
            status_labels=(),
        )
        dep = _dependency_task()
        run_state = RunState(active_worktrees={})
        ctx = _ctx(
            tasks_by_issue={1: task, 2: dep},
            prior_parent_merge_completed_issue_numbers=frozenset({dep.issue_number}),
        )
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            apply=True,
        )

        with (
            patch("fake_forge_proxy.active_fake_forge.remove_label") as mock_remove,
            patch("fake_forge_proxy.active_fake_forge.add_label") as mock_add,
        ):
            result = _handle_blocked_recompute_recovery(
                _IssuesStub(
                    [_issue(1, labels=("status:blocked-recompute", "status:blocked"))]
                ),
                run_state,
                ctx,
                config,
            )

        assert mock_remove.call_args_list == [
            ((1, "status:blocked-recompute"),),
            ((1, "status:blocked"),),
        ]
        mock_add.assert_called_once_with(1, "status:queued")
        assert result == [{"issue_number": 1, "subtask_id": "task-a"}]

    def _normal_promotion_dependency_result(
        self, tmp_path, *, dependency_labels, confirmed=()
    ):
        subject = _task(
            issue_number=1,
            subtask_id="subject",
            status_labels=("status:blocked",),
            depends_on=("dep",),
            parent_number=100,
        )
        dependency = _task(
            issue_number=2,
            subtask_id="dep",
            status_labels=dependency_labels,
            parent_number=100,
        )
        run_state = RunState(active_worktrees={})
        ctx = _ctx(
            tasks_by_issue={1: subject, 2: dependency},
            run_state=run_state,
            prior_parent_merge_completed_issue_numbers=frozenset(confirmed),
        )
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            apply=False,
        )
        adapter = _DispatchConsistencyAdapter(
            config,
            run_state,
            _IssuesStub([]),
            ctx,
            fresh=False,
        )
        desired = adapter.derive(
            ObservedRepositoryState(
                repository_id="test-repository",
                observed_at=datetime(2026, 9, 13, tzinfo=UTC),
            )
        )
        (fact,) = (
            fact
            for fact in desired.facts
            if fact.scope is ConsistencyScope.TASK
            and fact.subject_id == "1"
            and fact.name == DESIRED_DEPENDENCIES_RESOLVED
        )
        return fact.value

    @pytest.mark.parametrize(
        ("dependency_labels", "confirmed", "expected"),
        (
            (("status:done",), (), True),
            (("status:not-needed",), (), True),
            (("status:queued",), {2}, True),
            (("status:queued",), (), False),
            (("status:done", "status:queued"), (), False),
        ),
    )
    def test_normal_promotion_uses_dependency_assessment_policy(
        self, tmp_path, dependency_labels, confirmed, expected
    ):
        assert (
            self._normal_promotion_dependency_result(
                tmp_path,
                dependency_labels=dependency_labels,
                confirmed=confirmed,
            )
            is expected
        )

    def test_adds_queued_before_removing_blocked(self, tmp_path):
        # #381: status:blocked-recompute除去後もstatus:blockedが併存する間は
        # 安全だが、最終的にstatus:queuedへ遷移する際は、途中でクラッシュ
        # してもIssueが必ずいずれかのstatus:*ラベルを持ち続けるよう、
        # addがremove(status:blocked)より先に呼ばれなければならない。
        task = _task(
            issue_number=1,
            subtask_id="task-a",
            depends_on=("task-x",),
            status_labels=(),
        )
        dep = _dependency_task()
        run_state = RunState(active_worktrees={})
        ctx = _ctx(
            tasks_by_issue={1: task, 2: dep},
            prior_parent_merge_completed_issue_numbers=frozenset({dep.issue_number}),
        )
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            apply=True,
        )
        call_order: list[tuple[str, str]] = []

        with (
            patch(
                "fake_forge_proxy.active_fake_forge.remove_label",
                side_effect=lambda issue, label: call_order.append(("remove", label)),
            ),
            patch(
                "fake_forge_proxy.active_fake_forge.add_label",
                side_effect=lambda issue, label: call_order.append(("add", label)),
            ),
        ):
            _handle_blocked_recompute_recovery(
                _IssuesStub(
                    [_issue(1, labels=("status:blocked-recompute", "status:blocked"))]
                ),
                run_state,
                ctx,
                config,
            )

        assert call_order == [
            ("remove", "status:blocked-recompute"),
            ("add", "status:queued"),
            ("remove", "status:blocked"),
        ]

    def test_stays_blocked_when_dependency_still_pending(self, tmp_path):
        task = _task(
            issue_number=1,
            subtask_id="task-a",
            depends_on=("task-x",),
            status_labels=(),
        )
        run_state = RunState(active_worktrees={})
        ctx = _ctx(tasks_by_issue={1: task})
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            apply=True,
        )

        with (
            patch("fake_forge_proxy.active_fake_forge.remove_label") as mock_remove,
            patch("fake_forge_proxy.active_fake_forge.add_label") as mock_add,
        ):
            result = _handle_blocked_recompute_recovery(
                _IssuesStub(
                    [_issue(1, labels=("status:blocked-recompute", "status:blocked"))]
                ),
                run_state,
                ctx,
                config,
            )

        mock_remove.assert_called_once_with(1, "status:blocked-recompute")
        mock_add.assert_not_called()
        assert result == []

    def test_dependency_resolved_via_confirmed_context_fact(self, tmp_path):
        task = _task(
            issue_number=1,
            subtask_id="task-a",
            depends_on=("task-x",),
            status_labels=(),
        )
        dep = _dependency_task()
        run_state = RunState(active_worktrees={})
        ctx = _ctx(
            tasks_by_issue={1: task, 2: dep},
            prior_parent_merge_completed_issue_numbers=frozenset({dep.issue_number}),
        )
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            apply=True,
        )

        with (
            patch("fake_forge_proxy.active_fake_forge.remove_label") as mock_remove,
            patch("fake_forge_proxy.active_fake_forge.add_label") as mock_add,
        ):
            result = _handle_blocked_recompute_recovery(
                _IssuesStub(
                    [_issue(1, labels=("status:blocked-recompute", "status:blocked"))]
                ),
                run_state,
                ctx,
                config,
            )

        assert mock_remove.call_args_list == [
            ((1, "status:blocked-recompute"),),
            ((1, "status:blocked"),),
        ]
        mock_add.assert_called_once_with(1, "status:queued")
        assert result == [{"issue_number": 1, "subtask_id": "task-a"}]
