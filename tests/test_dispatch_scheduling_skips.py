"""#787: 起動候補から外れたタスクの理由（SkipRecord）導出のテスト。

`tests/test_dispatch_cycle.py`の肥大化解消のため分割している。
"""

import tempfile
from pathlib import Path

import pytest

from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.cycle_actions import CycleActionAdapter
from orchestune.dispatch.dependency_resolution import (
    TaskDependencies,
    UnresolvedDependency,
)
from orchestune.dispatch.locks import ExternalLockConflict, ExternalLockScanResult
from orchestune.dispatch.phase_scheduling import _determine_candidate_tasks
from orchestune.dispatch.state import ActiveWorktree, RunState, TaskReclaimRecord
from orchestune.dispatch.summary import (
    REASON_DEPENDENCY,
    REASON_EXTERNAL_LOCK,
    REASON_REVIEW_TIMEOUT_BACKOFF,
    merge_skips,
)
from orchestune.models import Task
from orchestune.task_metadata import CycleTask
from tests.dispatch_test_support import make_test_cycle_context

tmp_path = Path(tempfile.mkdtemp(prefix="orchestune-test-state-"))


def _task(**overrides):
    defaults = dict(
        issue_number=1,
        subtask_id="task-a",
        footprint=(),
        symbols=(),
        risk=False,
        priority="medium",
        progress_partial=False,
        status_labels=("status:queued",),
        created_at="2026-01-01T00:00:00+00:00",
        depends_on=(),
    )
    defaults.update(overrides)
    return Task(**defaults)


def _ctx(**overrides):
    return make_test_cycle_context(
        state_root=tmp_path, resolve_dependencies=True, **overrides
    )


class TestDetermineCandidateTaskSkips:
    """#787 / PR#789レビュー(Codex P2): 未選定理由の取り違えを防ぐ。"""

    def test_newly_locked_task_keeps_its_external_lock_reason(self, fake_forge):
        """新規ロックされたタスクは`queued_candidates`から外れるが、それを
        「actor権限の未確認で落ちた」と読み替えてはいけない。衝突の詳細が失われる。"""
        task = _task(
            issue_number=695, subtask_id="task-a", status_labels=("status:queued",)
        )
        lock_result = ExternalLockScanResult(
            to_lock=[task],
            to_unlock=[],
            conflicts={
                695: (
                    ExternalLockConflict(
                        kind="branch",
                        source="fix/issue-777-branch-naming",
                        files=("tests/conftest.py",),
                    ),
                )
            },
        )

        _, _, skips = _determine_candidate_tasks(
            _ctx(tasks_by_issue={695: task}),
            lock_result,
        )

        # 生の`skips`はJSONレポートとevents.jsonlにそのまま載るため、誤った
        # 理由の記録がここに混ざること自体を許さない。
        assert [(r.issue_number, r.reason) for r in skips] == [
            (695, REASON_EXTERNAL_LOCK)
        ]
        merged = merge_skips(skips)
        assert merged[0].detail == "fix/issue-777-branch-naming [tests/conftest.py]"

    def test_blocked_task_without_unresolved_dependencies_is_not_called_dependency(
        self, fake_forge
    ):
        """`status:blocked`は base-branch-red や起動失敗でも付く。依存待ちで
        ないタスクを「依存タスク未完了」と報告すると診断を誤らせる。"""
        task = _task(
            issue_number=1,
            subtask_id="task-a",
            status_labels=("status:blocked",),
            depends_on=(),
        )

        _, _, skips = _determine_candidate_tasks(
            _ctx(tasks_by_issue={1: task}),
            ExternalLockScanResult(to_lock=[], to_unlock=[]),
        )

        assert [record.reason for record in skips] == []

    def test_blocked_task_with_unresolved_dependencies_reports_what_it_waits_for(
        self, fake_forge
    ):
        upstream = _task(
            issue_number=695,
            subtask_id="task-a",
            parent_number=100,
            status_labels=("status:in-progress",),
        )
        task = _task(
            issue_number=696,
            subtask_id="task-b",
            status_labels=("status:blocked",),
            depends_on=("task-a",),
            parent_number=100,
        )

        _, _, skips = _determine_candidate_tasks(
            _ctx(tasks_by_issue={695: upstream, 696: task}),
            ExternalLockScanResult(to_lock=[], to_unlock=[]),
        )

        assert [(r.reason, r.detail) for r in skips] == [
            (REASON_DEPENDENCY, "waiting: #695")
        ]

    def test_blocked_task_with_unknown_parent_reports_diagnostic_reason(
        self, fake_forge
    ):
        """#799: 親不明の本文依存は「未解決」として、理由付きで診断に残す。"""
        task = _task(
            issue_number=696,
            subtask_id="task-b",
            status_labels=("status:blocked",),
            depends_on=("task-a",),
            parent_number=None,
        )

        _, _, skips = _determine_candidate_tasks(
            _ctx(tasks_by_issue={696: task}),
            ExternalLockScanResult(to_lock=[], to_unlock=[]),
        )

        assert [(r.reason, r.detail) for r in skips] == [
            (REASON_DEPENDENCY, "waiting: task-a (unknown-parent)")
        ]

    def test_queued_task_with_missing_assessment_fails_closed(self, fake_forge):
        task = _task(issue_number=5, status_labels=("status:queued",))

        candidates, _, skips = _determine_candidate_tasks(
            _ctx(tasks_by_issue={5: task}, dependency_resolution={}),
            ExternalLockScanResult(to_lock=[], to_unlock=[]),
        )

        assert candidates == []
        assert [(record.reason, record.detail) for record in skips] == [
            (REASON_DEPENDENCY, "dependency assessment unavailable: #5")
        ]

    def test_queued_unresolved_diagnostic_preserves_reason_and_candidates(
        self, fake_forge
    ):
        task = _task(issue_number=5, status_labels=("status:queued",))
        resolution = {
            5: TaskDependencies(
                unresolved=(
                    UnresolvedDependency(
                        raw="mystery", reason="custom-reason", candidates=(9, 3)
                    ),
                )
            )
        }

        candidates, _, skips = _determine_candidate_tasks(
            _ctx(tasks_by_issue={5: task}, dependency_resolution=resolution),
            ExternalLockScanResult(to_lock=[], to_unlock=[]),
        )

        assert candidates == []
        assert skips[0].detail == "waiting: mystery (custom-reason: #3, #9)"

    @pytest.mark.parametrize(
        ("dependency_resolution", "expected_detail"),
        [
            (
                {
                    3: TaskDependencies(resolved=(2,)),
                    2: TaskDependencies(resolved=(1,)),
                    1: TaskDependencies(),
                },
                "dependency #2: waiting: #1",
            ),
            (
                {3: TaskDependencies(resolved=(2,))},
                "dependency #2: dependency assessment unavailable: #2",
            ),
        ],
        ids=["grand-incomplete", "grand-assessment-unavailable"],
    )
    def test_blocked_stack_rejection_reports_grand_dependency_reason(
        self, fake_forge, dependency_resolution, expected_detail
    ):
        task_a = _task(
            issue_number=3,
            status_labels=("status:blocked",),
            native_depends_on=(2,),
        )
        task_b = _task(
            issue_number=2,
            status_labels=("status:in-progress",),
            native_depends_on=(1,),
        )
        task_c = _task(issue_number=1, status_labels=("status:in-progress",))
        ctx = _ctx(
            tasks_by_issue={1: task_c, 2: task_b, 3: task_a},
            dependency_resolution=dependency_resolution,
            ci_passed_pr_issue_numbers={2},
            branch_by_issue_number={2: "feat/issue-2-b"},
        )

        candidates, _, skips = _determine_candidate_tasks(
            ctx,
            ExternalLockScanResult(to_lock=[], to_unlock=[]),
        )

        assert candidates == []
        assert skips[0].detail == expected_detail


class TestExternalLockSkipScope:
    """PR#789レビュー(Codex P2): 同じサイクルでロックを外すタスクを
    「外部ロックで見送った」と報告しない。"""

    def test_task_being_unlocked_is_not_reported_as_locked(self, fake_forge):
        """`lock_changes`が同じサイクルでロック解除を報告しているのに、
        未選定一覧では「ロック中」と出るのは矛盾している。"""
        task = _task(issue_number=1, status_labels=("status:external-lock",))

        _, _, skips = _determine_candidate_tasks(
            _ctx(tasks_by_issue={1: task}),
            ExternalLockScanResult(to_lock=[], to_unlock=[task], conflicts={}),
        )

        assert skips == []

    def test_terminal_task_with_a_stale_lock_is_not_a_skipped_candidate(
        self, fake_forge
    ):
        """`status:done`のタスクはそもそも起動候補ではない。"""
        done_task = _task(
            issue_number=2, status_labels=("status:done", "status:external-lock")
        )

        _, _, skips = _determine_candidate_tasks(
            _ctx(tasks_by_issue={2: done_task}),
            ExternalLockScanResult(to_lock=[], to_unlock=[done_task], conflicts={}),
        )

        assert skips == []

    def test_task_that_stays_locked_is_still_reported(self, fake_forge):
        task = _task(issue_number=695, status_labels=("status:external-lock",))
        conflicts = {
            695: (
                ExternalLockConflict(
                    kind="branch", source="feat/x", files=("tests/conftest.py",)
                ),
            )
        }

        _, _, skips = _determine_candidate_tasks(
            _ctx(tasks_by_issue={695: task}),
            ExternalLockScanResult(to_lock=[], to_unlock=[], conflicts=conflicts),
        )

        assert [(r.issue_number, r.reason) for r in skips] == [
            (695, REASON_EXTERNAL_LOCK)
        ]
        assert skips[0].detail == "feat/x [tests/conftest.py]"


class TestInProgressTasksAreNotSkipCandidates:
    """PR#789レビュー(Codex P2): 実行中のタスクは起動候補ではない。"""

    def _conflicts(self, number):
        return {
            number: (
                ExternalLockConflict(kind="branch", source="feat/x", files=("a.py",)),
            )
        }

    def test_in_progress_label_excludes_the_task(self, fake_forge):
        task = _task(
            issue_number=5,
            status_labels=("status:in-progress", "status:external-lock"),
        )

        _, _, skips = _determine_candidate_tasks(
            _ctx(tasks_by_issue={5: task}),
            ExternalLockScanResult(
                to_lock=[], to_unlock=[], conflicts=self._conflicts(5)
            ),
        )

        assert skips == []

    def test_active_worktree_excludes_the_task(self, fake_forge):
        """ラベルの反映が遅れていても、run_stateに実行記録があれば除外する。"""
        task = _task(issue_number=5, status_labels=("status:external-lock",))
        run_state = RunState(
            active_worktrees={
                "5": ActiveWorktree(
                    issue_number=5,
                    branch="claude/issue-5-task-a",
                    worktree_path="worktrees/w5",
                    pid=1,
                    started_at=1.0,
                    declared_footprint=(),
                )
            }
        )

        _, _, skips = _determine_candidate_tasks(
            _ctx(tasks_by_issue={5: task}, run_state=run_state),
            ExternalLockScanResult(
                to_lock=[], to_unlock=[], conflicts=self._conflicts(5)
            ),
        )

        assert skips == []

    def test_review_timeout_backoff_skip(self, fake_forge):
        """review-timeoutの指数バックオフ待ちタスクがREASON_REVIEW_TIMEOUT_BACKOFFでスキップされる。"""
        fake_forge.get_label_actor.return_value = "authorized-user"
        fake_forge.get_actor_permission.return_value = "write"
        config = DispatcherConfig(
            parent_issue_number=100,
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            forge=fake_forge,
        )
        task = _task(issue_number=5, status_labels=("status:queued",))
        run_state = RunState(
            task_reclaim_counts={
                5: TaskReclaimRecord(
                    review_timeout_retry_count=1,
                    review_timeout_retry_at=100.0,
                )
            }
        )

        # now=50.0 (バックオフ期間中) -> スキップされる
        ctx = _ctx(tasks_by_issue={5: task}, run_state=run_state, config=config)
        candidates, _, skips = _determine_candidate_tasks(
            ctx,
            ExternalLockScanResult(to_lock=[], to_unlock=[], conflicts={}),
        )
        assert candidates == [CycleTask.from_task(task)]
        assert skips == []
        adapter = CycleActionAdapter(run_state, config, now=50.0)
        adapter.bind_context(ctx)
        selection = adapter.select_tasks(tuple(candidates))
        assert selection.selected == []
        backoff = [
            decision
            for decision in selection.decisions
            if decision.reason == REASON_REVIEW_TIMEOUT_BACKOFF
        ]
        assert [decision.issue_number for decision in backoff] == [5]

        # now=150.0 (バックオフ経過後) -> 起動候補に残る
        candidates_after, _, skips_after = _determine_candidate_tasks(
            _ctx(tasks_by_issue={5: task}, run_state=run_state, config=config),
            ExternalLockScanResult(to_lock=[], to_unlock=[], conflicts={}),
        )
        assert len(candidates_after) == 1
        assert candidates_after[0].issue_number == 5
        assert skips_after == []
