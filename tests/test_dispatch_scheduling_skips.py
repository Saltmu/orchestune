"""#787: 起動候補から外れたタスクの理由（SkipRecord）導出のテスト。

`tests/test_dispatch_cycle.py`の肥大化解消のため分割している。
"""

import tempfile
from pathlib import Path

from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.cycle_context import IssuesByStatus
from orchestune.dispatch.locks import ExternalLockConflict, ExternalLockScanResult
from orchestune.dispatch.phase_scheduling import _determine_candidate_tasks
from orchestune.dispatch.rules import CycleContext
from orchestune.dispatch.state import ActiveWorktree, RunState, TaskReclaimRecord
from orchestune.dispatch.summary import (
    REASON_DEPENDENCY,
    REASON_EXTERNAL_LOCK,
    REASON_REVIEW_TIMEOUT_BACKOFF,
    merge_skips,
)
from orchestune.models import IssueRecord, Task

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


def _issue(number, labels=(), state="OPEN"):
    return IssueRecord(
        number=number,
        title=f"Issue {number}",
        body="",
        labels=labels,
        created_at="2026-01-01T00:00:00+00:00",
        state=state,
    )


def _ctx(**overrides):
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
        ),
    )
    defaults.update(overrides)
    return CycleContext(**defaults)


class TestDetermineCandidateTaskSkips:
    """#787 / PR#789レビュー(Codex P2): 未選定理由の取り違えを防ぐ。"""

    def test_newly_locked_task_keeps_its_external_lock_reason(self, fake_forge):
        """新規ロックされたタスクは`queued_candidates`から外れるが、それを
        「actor権限の未確認で落ちた」と読み替えてはいけない。衝突の詳細が失われる。"""
        task = _task(
            issue_number=695, subtask_id="task-a", status_labels=("status:queued",)
        )
        issues = IssuesByStatus(
            queued=[_issue(695, labels=("status:queued",))],
            locked=[],
            in_progress=[],
            blocked=[],
            done=[],
            not_needed=[],
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
            issues,
            lock_result,
            set(),
            False,
            now=0.0,
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
        issues = IssuesByStatus(
            queued=[],
            locked=[],
            in_progress=[],
            blocked=[_issue(1, labels=("status:blocked",))],
            done=[],
            not_needed=[],
        )

        _, _, skips = _determine_candidate_tasks(
            _ctx(tasks_by_issue={1: task}),
            issues,
            ExternalLockScanResult(to_lock=[], to_unlock=[]),
            set(),
            False,
            now=0.0,
        )

        assert [record.reason for record in skips] == []

    def test_blocked_task_with_unresolved_dependencies_reports_what_it_waits_for(
        self, fake_forge
    ):
        task = _task(
            issue_number=696,
            subtask_id="task-b",
            status_labels=("status:blocked",),
            depends_on=("task-a",),
        )
        issues = IssuesByStatus(
            queued=[],
            locked=[],
            in_progress=[],
            blocked=[_issue(696, labels=("status:blocked",))],
            done=[],
            not_needed=[],
        )

        _, _, skips = _determine_candidate_tasks(
            _ctx(
                tasks_by_issue={696: task},
                issue_number_by_subtask_id={"task-a": 695},
            ),
            issues,
            ExternalLockScanResult(to_lock=[], to_unlock=[]),
            set(),
            False,
            now=0.0,
        )

        assert [(r.reason, r.detail) for r in skips] == [
            (REASON_DEPENDENCY, "waiting: #695")
        ]


class TestExternalLockSkipScope:
    """PR#789レビュー(Codex P2): 同じサイクルでロックを外すタスクを
    「外部ロックで見送った」と報告しない。"""

    def _issues(self, locked_numbers):
        return IssuesByStatus(
            queued=[],
            locked=[
                _issue(number, labels=("status:external-lock",))
                for number in locked_numbers
            ],
            in_progress=[],
            blocked=[],
            done=[],
            not_needed=[],
        )

    def test_task_being_unlocked_is_not_reported_as_locked(self, fake_forge):
        """`lock_changes`が同じサイクルでロック解除を報告しているのに、
        未選定一覧では「ロック中」と出るのは矛盾している。"""
        task = _task(issue_number=1, status_labels=("status:external-lock",))

        _, _, skips = _determine_candidate_tasks(
            _ctx(tasks_by_issue={1: task}),
            self._issues([1]),
            ExternalLockScanResult(to_lock=[], to_unlock=[task], conflicts={}),
            set(),
            False,
            now=0.0,
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
            self._issues([2]),
            ExternalLockScanResult(to_lock=[], to_unlock=[done_task], conflicts={}),
            set(),
            False,
            now=0.0,
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
            self._issues([695]),
            ExternalLockScanResult(to_lock=[], to_unlock=[], conflicts=conflicts),
            set(),
            False,
            now=0.0,
        )

        assert [(r.issue_number, r.reason) for r in skips] == [
            (695, REASON_EXTERNAL_LOCK)
        ]
        assert skips[0].detail == "feat/x [tests/conftest.py]"


class TestInProgressTasksAreNotSkipCandidates:
    """PR#789レビュー(Codex P2): 実行中のタスクは起動候補ではない。"""

    def _locked_issues(self, number):
        return IssuesByStatus(
            queued=[],
            locked=[_issue(number, labels=("status:external-lock",))],
            in_progress=[],
            blocked=[],
            done=[],
            not_needed=[],
        )

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
            self._locked_issues(5),
            ExternalLockScanResult(
                to_lock=[], to_unlock=[], conflicts=self._conflicts(5)
            ),
            set(),
            False,
            now=0.0,
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
            self._locked_issues(5),
            ExternalLockScanResult(
                to_lock=[], to_unlock=[], conflicts=self._conflicts(5)
            ),
            set(),
            False,
            now=0.0,
        )

        assert skips == []

    def test_review_timeout_backoff_skip(self, fake_forge):
        """review-timeoutの指数バックオフ待ちタスクがREASON_REVIEW_TIMEOUT_BACKOFFでスキップされる。"""
        fake_forge.get_label_actor.return_value = "authorized-user"
        fake_forge.get_actor_permission.return_value = "write"
        config = DispatcherConfig(
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
        issues = IssuesByStatus(
            queued=[_issue(5, ("status:queued",))],
            locked=[],
            in_progress=[],
            blocked=[],
            done=[],
            not_needed=[],
        )
        candidates, _, skips = _determine_candidate_tasks(
            _ctx(tasks_by_issue={5: task}, run_state=run_state, config=config),
            issues,
            ExternalLockScanResult(to_lock=[], to_unlock=[], conflicts={}),
            set(),
            False,
            now=50.0,
        )
        assert candidates == []
        assert len(skips) == 1
        assert skips[0].issue_number == 5
        assert skips[0].reason == REASON_REVIEW_TIMEOUT_BACKOFF

        # now=150.0 (バックオフ経過後) -> 起動候補に残る
        candidates_after, _, skips_after = _determine_candidate_tasks(
            _ctx(tasks_by_issue={5: task}, run_state=run_state, config=config),
            issues,
            ExternalLockScanResult(to_lock=[], to_unlock=[], conflicts={}),
            set(),
            False,
            now=150.0,
        )
        assert len(candidates_after) == 1
        assert candidates_after[0].issue_number == 5
        assert skips_after == []
