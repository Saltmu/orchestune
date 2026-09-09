"""同一サイクル内で確定した完了の組み立て（#859）。

`_same_cycle_completions`は、active worktreeの完了検知と検証済み先行マージという
2つの確定経路を1か所で合流させる唯一の入口である。消費側ごとに片方だけを
合流させる書き方に戻ると、先行マージ完了が`status_repair`系へ届かず、依存元が
次サイクルまで`status:blocked`のまま据え置かれる回帰が再発する。
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from orchestune.consistency.supervisor import ConsistencyMode
from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.cycle import (
    _completed_issue_numbers,
    _finish_consistency_runtime,
    _RepairCycleState,
    _same_cycle_completions,
)
from orchestune.dispatch.cycle_report import CycleReport
from orchestune.dispatch.rules import CycleContext
from orchestune.dispatch.state import RunState
from orchestune.models import Task

tmp_path = Path(tempfile.mkdtemp(prefix="orchestune-test-same-cycle-"))


def _task(issue_number: int, *, subtask_id: str = "task-a", status_labels=()) -> Task:
    return Task(
        issue_number=issue_number,
        subtask_id=subtask_id,
        footprint=(),
        symbols=(),
        risk=False,
        priority="medium",
        progress_partial=False,
        status_labels=tuple(status_labels),
        created_at="2026-01-01T00:00:00+00:00",
        parent_number=100,
    )


def _ctx(**overrides: Any) -> CycleContext:
    defaults: dict[str, Any] = dict(
        run_state=RunState(active_worktrees={}),
        tasks_by_issue={},
        issue_number_by_subtask_id={},
        dependency_resolution={},
        done_issue_numbers=set(),
        ci_passed_pr_issue_numbers=set(),
        changes_requested_issue_numbers=set(),
        branch_by_issue_number={},
        prs=[],
        pr_by_branch={},
        config=DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
        ),
    )
    defaults.update(overrides)
    return CycleContext(**defaults)


class TestSameCycleCompletions:
    def test_unions_active_worktree_and_prior_parent_merge_sources(self):
        ctx = _ctx(prior_parent_merge_completed_issue_numbers=frozenset({7}))

        assert _same_cycle_completions(ctx, {3}) == frozenset({3, 7})

    def test_deduplicates_an_issue_confirmed_by_both_sources(self):
        ctx = _ctx(prior_parent_merge_completed_issue_numbers=frozenset({3}))

        assert _same_cycle_completions(ctx, {3}) == frozenset({3})

    def test_returns_each_source_alone(self):
        active_only = _ctx()
        prior_only = _ctx(prior_parent_merge_completed_issue_numbers=frozenset({7}))

        assert _same_cycle_completions(active_only, {3}) == frozenset({3})
        assert _same_cycle_completions(prior_only, ()) == frozenset({7})

    def test_is_empty_without_either_source(self):
        assert _same_cycle_completions(_ctx(), ()) == frozenset()


class TestCompletedIssueNumbers:
    def test_includes_prior_parent_merge_completion_without_done_label(self):
        """#859の回帰テスト。

        `tasks_by_issue`は先行マージが`status:done`を付与する前のIssueから
        構築されるため、依存先タスクのラベルは`status:blocked`のままである。
        それでも実効的には完了しているので、完了集合へ入らなければならない。
        """
        ctx = _ctx(
            tasks_by_issue={1: _task(1, status_labels=("status:blocked",))},
            prior_parent_merge_completed_issue_numbers=frozenset({1}),
        )

        assert _completed_issue_numbers(ctx, ()) == {1}

    def test_excludes_issues_only_held_by_indeterminate_evidence(self):
        """証拠不定で保留されただけのタスクは完了扱いにしない（fail-closed）。"""
        ctx = _ctx(
            tasks_by_issue={1: _task(1, status_labels=("status:blocked",))},
            prior_parent_merge_hold_issue_numbers=frozenset({1}),
        )

        assert _completed_issue_numbers(ctx, ()) == set()

    def test_preserves_label_derived_membership(self):
        """#823でまとめて統一するまでの現行挙動を固定する。

        ラベル由来の完了は`status:done`/`status:not-needed`の両方を含み、
        `subtask_id`を持たないタスクは対象外である。この2軸の統一は
        #823の`DependencyAssessment`導入と同時に行うため、本Issueでは変えない。
        """
        ctx = _ctx(
            tasks_by_issue={
                1: _task(1, status_labels=("status:done",)),
                2: _task(2, subtask_id="task-b", status_labels=("status:not-needed",)),
                3: _task(3, subtask_id="", status_labels=("status:done",)),
                4: _task(4, subtask_id="task-d", status_labels=("status:blocked",)),
            }
        )

        assert _completed_issue_numbers(ctx, ()) == {1, 2}

    def test_merges_label_derived_and_same_cycle_sources(self):
        ctx = _ctx(
            tasks_by_issue={
                1: _task(1, status_labels=("status:done",)),
                2: _task(2, subtask_id="task-b", status_labels=("status:blocked",)),
                3: _task(3, subtask_id="task-c", status_labels=("status:in-progress",)),
            },
            prior_parent_merge_completed_issue_numbers=frozenset({2}),
        )

        assert _completed_issue_numbers(ctx, {3}) == {1, 2, 3}


class TestFinalConsistencyRepairExecutor:
    """サイクル終端の修復も、他の消費側と同じ実効完了を見る（#859）。"""

    def _run_finish(self, ctx, same_cycle_completions):
        runtime = MagicMock()
        report = CycleReport(
            selected=[],
            quota_slots_available=0,
            lock_changes={"to_lock": [], "to_unlock": []},
            deviation_events=[],
            completion_events=[],
            promotion_events=[],
            applied=False,
        )
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            consistency_mode=ConsistencyMode.REPAIR,
            apply=False,
        )
        with patch(
            "orchestune.dispatch.cycle._DispatchRepairExecutor"
        ) as executor_factory:
            _finish_consistency_runtime(
                runtime,
                report,
                ctx,
                0.0,
                config,
                _RepairCycleState(),
                same_cycle_completions,
            )
        return executor_factory

    def test_receives_persisted_and_same_cycle_completions(self):
        ctx = _ctx(done_issue_numbers={1})

        executor_factory = self._run_finish(ctx, frozenset({2}))

        assert executor_factory.call_args.kwargs[
            "completed_issue_numbers"
        ] == frozenset({1, 2})

    def test_defaults_to_persisted_completions_only(self):
        ctx = _ctx(done_issue_numbers={1})

        executor_factory = self._run_finish(ctx, frozenset())

        assert executor_factory.call_args.kwargs[
            "completed_issue_numbers"
        ] == frozenset({1})
