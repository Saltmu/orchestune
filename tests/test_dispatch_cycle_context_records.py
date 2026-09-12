"""Issue #868: `record_completion` / `record_launch` / `record_transition`と
遷移表を固定する（Issue本文F段5〜7）。

record APIは外部I/Oを行わない。呼出側が既に成功確認した事実だけを反映し、
CONFLICT時は状態を一切変更しない。ここではAPI単体の整合性・冪等性・入力
検証だけを検証し、「実際にいつrecordを呼ぶか」(成功/失敗ごとの接続)は
#871〜#873の統合テストが担当する。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.cycle_context_state import (
    REASON_EXECUTION_MISMATCH,
    REASON_INVALID_LAUNCH,
    REASON_INVALID_TRANSITION,
    REASON_LAUNCH_MISMATCH,
    REASON_STALE_OBSERVATION,
    REASON_TERMINAL_STATE,
    REASON_UNKNOWN_ISSUE,
    RecordStatus,
)
from orchestune.dispatch.rules import CycleContext
from orchestune.dispatch.state import ActiveWorktree, RunState
from orchestune.labels import StatusLabel
from orchestune.models import Task

_TMP = Path(tempfile.mkdtemp(prefix="orchestune-test-cycle-context-records-"))


def _task(issue_number, **overrides):
    defaults = dict(
        issue_number=issue_number,
        subtask_id=f"task-{issue_number}",
        footprint=(),
        symbols=(),
        risk=False,
        priority="medium",
        progress_partial=False,
        status_labels=(StatusLabel.QUEUED,),
        created_at="2026-01-01T00:00:00Z",
        issue_state="OPEN",
    )
    defaults.update(overrides)
    return Task(**defaults)


def _active(issue_number, **overrides):
    defaults = dict(
        issue_number=issue_number,
        branch=f"claude/issue-{issue_number}-task",
        worktree_path=f"worktrees/w{issue_number}",
        pid=1000 + issue_number,
        started_at=1_700_000_000.0,
        declared_footprint=(),
    )
    defaults.update(overrides)
    return ActiveWorktree(**defaults)


def _ctx(**overrides):
    defaults = dict(
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
            events_log_path=_TMP / "events.jsonl",
            run_state_path=_TMP / "run_state.json",
            worktree_root=_TMP / "worktrees",
        ),
    )
    defaults.update(overrides)
    return CycleContext(**defaults)


class TestRecordCompletion:
    """F段5: `record_completion`。"""

    def test_unknown_issue_is_conflict(self):
        ctx = _ctx()
        result = ctx.record_completion(999)
        assert result.status == RecordStatus.CONFLICT
        assert result.reason == REASON_UNKNOWN_ISSUE

    def test_known_incomplete_issue_is_applied(self):
        ctx = _ctx(tasks_by_issue={1: _task(1, status_labels=(StatusLabel.QUEUED,))})
        result = ctx.record_completion(1)
        assert result.status == RecordStatus.APPLIED
        assert result.reason is None
        assert ctx.is_effectively_done(1) is True
        assert StatusLabel.DONE in ctx.task(1).status_labels

    def test_already_done_is_noop(self):
        ctx = _ctx(tasks_by_issue={1: _task(1, status_labels=(StatusLabel.DONE,))})
        before = ctx.task(1)
        result = ctx.record_completion(1)
        assert result.status == RecordStatus.NOOP
        assert result.reason is None
        assert ctx.task(1) == before

    def test_not_needed_is_noop_and_not_rewritten_to_done(self):
        ctx = _ctx(
            tasks_by_issue={1: _task(1, status_labels=(StatusLabel.NOT_NEEDED,))}
        )
        result = ctx.record_completion(1)
        assert result.status == RecordStatus.NOOP
        assert StatusLabel.NOT_NEEDED in ctx.task(1).status_labels
        assert StatusLabel.DONE not in ctx.task(1).status_labels

    def test_re_recording_same_completion_is_idempotent(self):
        ctx = _ctx(tasks_by_issue={1: _task(1, status_labels=(StatusLabel.QUEUED,))})
        first = ctx.record_completion(1)
        second = ctx.record_completion(1)
        assert first.status == RecordStatus.APPLIED
        assert second.status == RecordStatus.NOOP

    def test_completion_releases_in_progress_launch_from_candidate_exclusion_source(
        self,
    ):
        active = _active(1)
        ctx = _ctx(
            tasks_by_issue={1: _task(1, status_labels=(StatusLabel.IN_PROGRESS,))},
            run_state=RunState(active_worktrees={"1": active}),
        )
        assert ctx.record_completion(1).status == RecordStatus.APPLIED
        assert ctx.launch_fact(1) is not None  # 起動事実自体は履歴として残る
        assert ctx.is_effectively_done(1) is True

    def test_stale_ci_and_changes_requested_observations_do_not_block_completion(self):
        ctx = _ctx(
            tasks_by_issue={1: _task(1, status_labels=(StatusLabel.QUEUED,))},
            changes_requested_issue_numbers={1},
        )
        result = ctx.record_completion(1)
        assert result.status == RecordStatus.APPLIED
        assert ctx.is_effectively_done(1) is True
        # 完了でもCI/変更要求の観測自体は消えない(#867のCI_PASSED_UNMERGED等の
        # 優先順位判断は消費側の責務)。
        assert ctx.has_changes_requested(1) is True


class TestRecordLaunch:
    """F段6: `record_launch`。"""

    def test_unknown_issue_is_conflict(self):
        ctx = _ctx()
        result = ctx.record_launch(_active(999))
        assert result.status == RecordStatus.CONFLICT
        assert result.reason == REASON_UNKNOWN_ISSUE

    def test_first_launch_is_applied_and_reflected_in_task_and_launch_fact(self):
        ctx = _ctx(tasks_by_issue={1: _task(1, status_labels=(StatusLabel.QUEUED,))})
        active = _active(1)
        result = ctx.record_launch(active)
        assert result.status == RecordStatus.APPLIED
        assert ctx.launch_fact(1).branch == active.branch
        assert StatusLabel.IN_PROGRESS in ctx.task(1).status_labels

    def test_identical_re_record_is_noop(self):
        ctx = _ctx(tasks_by_issue={1: _task(1, status_labels=(StatusLabel.QUEUED,))})
        active = _active(1)
        ctx.record_launch(active)
        result = ctx.record_launch(_active(1))  # 全フィールド同一の別インスタンス
        assert result.status == RecordStatus.NOOP

    def test_same_attempt_id_different_fields_is_conflict(self):
        ctx = _ctx(tasks_by_issue={1: _task(1, status_labels=(StatusLabel.QUEUED,))})
        ctx.record_launch(_active(1, launch_attempt_id="attempt-1", branch="b1"))
        result = ctx.record_launch(
            _active(1, launch_attempt_id="attempt-1", branch="b2")
        )
        assert result.status == RecordStatus.CONFLICT
        assert result.reason == REASON_LAUNCH_MISMATCH

    def test_different_attempt_id_with_existing_active_launch_is_conflict(self):
        ctx = _ctx(tasks_by_issue={1: _task(1, status_labels=(StatusLabel.QUEUED,))})
        ctx.record_launch(_active(1, launch_attempt_id="attempt-1"))
        result = ctx.record_launch(_active(1, launch_attempt_id="attempt-2"))
        assert result.status == RecordStatus.CONFLICT
        assert result.reason == REASON_LAUNCH_MISMATCH

    def test_legacy_none_attempt_id_matches_only_on_full_field_equality(self):
        ctx = _ctx(tasks_by_issue={1: _task(1, status_labels=(StatusLabel.QUEUED,))})
        ctx.record_launch(_active(1, pid=42, started_at=1.0))
        same = ctx.record_launch(_active(1, pid=42, started_at=1.0))
        assert same.status == RecordStatus.NOOP

    def test_one_sided_attempt_id_is_never_treated_as_a_match(self):
        ctx = _ctx(tasks_by_issue={1: _task(1, status_labels=(StatusLabel.QUEUED,))})
        ctx.record_launch(_active(1, pid=42, started_at=1.0, launch_attempt_id=None))
        result = ctx.record_launch(
            _active(1, pid=42, started_at=1.0, launch_attempt_id="attempt-1")
        )
        assert result.status == RecordStatus.CONFLICT
        assert result.reason == REASON_LAUNCH_MISMATCH

    def test_invalid_launch_missing_branch_is_conflict(self):
        ctx = _ctx(tasks_by_issue={1: _task(1, status_labels=(StatusLabel.QUEUED,))})
        result = ctx.record_launch(_active(1, branch=""))
        assert result.status == RecordStatus.CONFLICT
        assert result.reason == REASON_INVALID_LAUNCH

    def test_invalid_launch_no_pid_and_no_external_id_is_conflict(self):
        ctx = _ctx(tasks_by_issue={1: _task(1, status_labels=(StatusLabel.QUEUED,))})
        result = ctx.record_launch(_active(1, pid=None, external_id=None))
        assert result.status == RecordStatus.CONFLICT
        assert result.reason == REASON_INVALID_LAUNCH

    def test_invalid_launch_phase_prepared_is_conflict(self):
        ctx = _ctx(tasks_by_issue={1: _task(1, status_labels=(StatusLabel.QUEUED,))})
        result = ctx.record_launch(_active(1, launch_phase="prepared"))
        assert result.status == RecordStatus.CONFLICT
        assert result.reason == REASON_INVALID_LAUNCH

    def test_launch_phase_launched_is_accepted(self):
        ctx = _ctx(tasks_by_issue={1: _task(1, status_labels=(StatusLabel.QUEUED,))})
        result = ctx.record_launch(
            _active(1, launch_phase="launched", external_id="ext-1")
        )
        assert result.status == RecordStatus.APPLIED

    def test_launch_on_effectively_done_issue_is_terminal_state_conflict(self):
        ctx = _ctx(tasks_by_issue={1: _task(1, status_labels=(StatusLabel.DONE,))})
        result = ctx.record_launch(_active(1))
        assert result.status == RecordStatus.CONFLICT
        assert result.reason == REASON_TERMINAL_STATE

    def test_launch_on_human_review_pending_issue_is_terminal_state_conflict(self):
        ctx = _ctx(
            tasks_by_issue={
                1: _task(1, status_labels=(StatusLabel.BLOCKED_HUMAN_REVIEW,))
            }
        )
        result = ctx.record_launch(_active(1))
        assert result.status == RecordStatus.CONFLICT
        assert result.reason == REASON_TERMINAL_STATE

    def test_ambiguous_initial_active_worktrees_always_conflict_launch_mismatch(self):
        ctx = _ctx(
            tasks_by_issue={1: _task(1, status_labels=(StatusLabel.IN_PROGRESS,))},
            run_state=RunState(
                active_worktrees={
                    "1a": _active(1, branch="claude/issue-1-a"),
                    "1b": _active(1, branch="claude/issue-1-b"),
                }
            ),
        )
        result = ctx.record_launch(_active(1, branch="claude/issue-1-a"))
        assert result.status == RecordStatus.CONFLICT
        assert result.reason == REASON_LAUNCH_MISMATCH

    def test_indeterminate_launch_phase_always_conflicts_launch_mismatch(self):
        ctx = _ctx(
            tasks_by_issue={1: _task(1, status_labels=(StatusLabel.IN_PROGRESS,))},
            run_state=RunState(
                active_worktrees={"1": _active(1, launch_phase="unknown")}
            ),
        )
        result = ctx.record_launch(_active(1))
        assert result.status == RecordStatus.CONFLICT
        assert result.reason == REASON_LAUNCH_MISMATCH

    def test_input_mutation_after_record_launch_does_not_change_result(self):
        ctx = _ctx(tasks_by_issue={1: _task(1, status_labels=(StatusLabel.QUEUED,))})
        active = _active(1, branch="claude/issue-1-original")
        ctx.record_launch(active)
        active.branch = "claude/issue-1-mutated-after"
        assert ctx.launch_fact(1).branch == "claude/issue-1-original"


class TestRecordTransition:
    """F段7: `record_transition`と遷移表の全行・代表拒否ケース。"""

    def test_unknown_issue_is_conflict(self):
        ctx = _ctx()
        result = ctx.record_transition(
            999,
            expected_labels=(StatusLabel.QUEUED,),
            verified_labels=(StatusLabel.BLOCKED,),
            execution_active=False,
        )
        assert result.status == RecordStatus.CONFLICT
        assert result.reason == REASON_UNKNOWN_ISSUE

    def test_blocked_to_queued_promotion_is_applied(self):
        ctx = _ctx(tasks_by_issue={1: _task(1, status_labels=(StatusLabel.BLOCKED,))})
        result = ctx.record_transition(
            1,
            expected_labels=(StatusLabel.BLOCKED,),
            verified_labels=(StatusLabel.QUEUED,),
            execution_active=False,
        )
        assert result.status == RecordStatus.APPLIED
        assert ctx.task(1).status_labels == (StatusLabel.QUEUED,)

    def test_queued_to_blocked_is_applied(self):
        ctx = _ctx(tasks_by_issue={1: _task(1, status_labels=(StatusLabel.QUEUED,))})
        result = ctx.record_transition(
            1,
            expected_labels=(StatusLabel.QUEUED,),
            verified_labels=(StatusLabel.BLOCKED,),
            execution_active=False,
        )
        assert result.status == RecordStatus.APPLIED

    def test_queued_to_in_progress_requires_existing_launch_fact(self):
        without_fact = _ctx(
            tasks_by_issue={1: _task(1, status_labels=(StatusLabel.QUEUED,))}
        )
        rejected = without_fact.record_transition(
            1,
            expected_labels=(StatusLabel.QUEUED,),
            verified_labels=(StatusLabel.IN_PROGRESS,),
            execution_active=True,
        )
        assert rejected.status == RecordStatus.CONFLICT
        assert rejected.reason == REASON_EXECUTION_MISMATCH

        # ラベルはまだQUEUEDのまま、起動事実だけが先に存在するケース
        # (Forgeラベル更新のみが遅延している場合)。
        with_fact = _ctx(
            tasks_by_issue={1: _task(1, status_labels=(StatusLabel.QUEUED,))},
            run_state=RunState(active_worktrees={"1": _active(1)}),
        )
        applied = with_fact.record_transition(
            1,
            expected_labels=(StatusLabel.QUEUED,),
            verified_labels=(StatusLabel.IN_PROGRESS,),
            execution_active=True,
        )
        assert applied.status == RecordStatus.APPLIED

    def test_in_progress_to_queued_invalidates_launch_fact(self):
        ctx = _ctx(
            tasks_by_issue={1: _task(1, status_labels=(StatusLabel.IN_PROGRESS,))},
            run_state=RunState(active_worktrees={"1": _active(1)}),
        )
        result = ctx.record_transition(
            1,
            expected_labels=(StatusLabel.IN_PROGRESS,),
            verified_labels=(StatusLabel.QUEUED,),
            execution_active=False,
        )
        assert result.status == RecordStatus.APPLIED
        assert ctx.launch_fact(1) is None

    def test_recovered_issue_permits_a_fresh_launch_afterwards(self):
        ctx = _ctx(
            tasks_by_issue={1: _task(1, status_labels=(StatusLabel.IN_PROGRESS,))},
            run_state=RunState(active_worktrees={"1": _active(1)}),
        )
        ctx.record_transition(
            1,
            expected_labels=(StatusLabel.IN_PROGRESS,),
            verified_labels=(StatusLabel.QUEUED,),
            execution_active=False,
        )
        result = ctx.record_launch(_active(1, branch="claude/issue-1-relaunch"))
        assert result.status == RecordStatus.APPLIED

    def test_escalation_from_queued_to_human_review_is_applied(self):
        ctx = _ctx(tasks_by_issue={1: _task(1, status_labels=(StatusLabel.QUEUED,))})
        result = ctx.record_transition(
            1,
            expected_labels=(StatusLabel.QUEUED,),
            verified_labels=(StatusLabel.BLOCKED_HUMAN_REVIEW,),
            execution_active=False,
        )
        assert result.status == RecordStatus.APPLIED

    def test_human_review_does_not_auto_release(self):
        ctx = _ctx(
            tasks_by_issue={
                1: _task(1, status_labels=(StatusLabel.BLOCKED_HUMAN_REVIEW,))
            }
        )
        result = ctx.record_transition(
            1,
            expected_labels=(StatusLabel.BLOCKED_HUMAN_REVIEW,),
            verified_labels=(StatusLabel.QUEUED,),
            execution_active=False,
        )
        assert result.status == RecordStatus.CONFLICT
        assert result.reason == REASON_INVALID_TRANSITION

    def test_ambiguous_primary_repair_to_queued_is_applied(self):
        ctx = _ctx(
            tasks_by_issue={
                1: _task(1, status_labels=(StatusLabel.QUEUED, StatusLabel.BLOCKED))
            }
        )
        result = ctx.record_transition(
            1,
            expected_labels=(StatusLabel.QUEUED, StatusLabel.BLOCKED),
            verified_labels=(StatusLabel.QUEUED,),
            execution_active=False,
        )
        assert result.status == RecordStatus.APPLIED

    def test_missing_primary_repair_to_blocked_is_applied(self):
        ctx = _ctx(tasks_by_issue={1: _task(1, status_labels=("priority:high",))})
        result = ctx.record_transition(
            1,
            expected_labels=("priority:high",),
            verified_labels=(StatusLabel.BLOCKED, "priority:high"),
            execution_active=False,
        )
        assert result.status == RecordStatus.APPLIED

    def test_terminal_rollback_to_non_terminal_is_terminal_state_conflict(self):
        ctx = _ctx(tasks_by_issue={1: _task(1, status_labels=(StatusLabel.DONE,))})
        result = ctx.record_transition(
            1,
            expected_labels=(StatusLabel.DONE,),
            verified_labels=(StatusLabel.QUEUED,),
            execution_active=False,
        )
        assert result.status == RecordStatus.CONFLICT
        assert result.reason == REASON_TERMINAL_STATE

    def test_same_primary_reflects_auxiliary_label_removal(self):
        ctx = _ctx(
            tasks_by_issue={
                1: _task(
                    1,
                    status_labels=(StatusLabel.QUEUED, StatusLabel.FORCE_SERIAL),
                )
            }
        )
        result = ctx.record_transition(
            1,
            expected_labels=(StatusLabel.QUEUED, StatusLabel.FORCE_SERIAL),
            verified_labels=(StatusLabel.QUEUED,),
            execution_active=False,
        )
        assert result.status == RecordStatus.APPLIED
        assert ctx.task(1).status_labels == (StatusLabel.QUEUED,)

    def test_exact_same_observation_is_noop(self):
        ctx = _ctx(tasks_by_issue={1: _task(1, status_labels=(StatusLabel.QUEUED,))})
        result = ctx.record_transition(
            1,
            expected_labels=(
                StatusLabel.BLOCKED,
            ),  # stale expected でも一致するならNOOP
            verified_labels=(StatusLabel.QUEUED,),
            execution_active=False,
        )
        assert result.status == RecordStatus.NOOP

    def test_same_labels_but_execution_ended_is_not_noop(self):
        ctx = _ctx(
            tasks_by_issue={1: _task(1, status_labels=(StatusLabel.IN_PROGRESS,))},
            run_state=RunState(active_worktrees={"1": _active(1)}),
        )
        result = ctx.record_transition(
            1,
            expected_labels=(StatusLabel.IN_PROGRESS,),
            verified_labels=(StatusLabel.IN_PROGRESS,),
            execution_active=False,
        )
        assert result.status == RecordStatus.APPLIED

    def test_stale_observation_when_expected_does_not_match_current(self):
        ctx = _ctx(tasks_by_issue={1: _task(1, status_labels=(StatusLabel.QUEUED,))})
        result = ctx.record_transition(
            1,
            expected_labels=(StatusLabel.BLOCKED,),
            verified_labels=(StatusLabel.IN_PROGRESS,),
            execution_active=False,
        )
        assert result.status == RecordStatus.CONFLICT
        assert result.reason == REASON_STALE_OBSERVATION

    def test_ambiguous_primary_with_human_review_lifecycle_cannot_repair(self):
        # 主状態が曖昧(複数)かつLifecycle=HUMAN_REVIEW(=OPENでない)の場合、
        # missing/conflict repairの対象外として拒否する。
        current = (
            StatusLabel.QUEUED,
            StatusLabel.BLOCKED,
            StatusLabel.BLOCKED_HUMAN_REVIEW,
        )
        ctx = _ctx(tasks_by_issue={1: _task(1, status_labels=current)})
        result = ctx.record_transition(
            1,
            expected_labels=current,
            verified_labels=(StatusLabel.QUEUED,),
            execution_active=False,
        )
        assert result.status == RecordStatus.CONFLICT
        assert result.reason == REASON_INVALID_TRANSITION

    def test_new_in_progress_entry_without_execution_active_is_invalid_transition(
        self,
    ):
        # execution_active=falseでIN_PROGRESSへ新規に入る遷移は拒否する
        # (起動事実の有無を問わず、起動整合性チェックより先に表外として扱う)。
        ctx = _ctx(
            tasks_by_issue={1: _task(1, status_labels=(StatusLabel.QUEUED,))},
            run_state=RunState(active_worktrees={"1": _active(1)}),
        )
        result = ctx.record_transition(
            1,
            expected_labels=(StatusLabel.QUEUED,),
            verified_labels=(StatusLabel.IN_PROGRESS,),
            execution_active=False,
        )
        assert result.status == RecordStatus.CONFLICT
        assert result.reason == REASON_INVALID_TRANSITION

    def test_multiple_primary_labels_in_verified_is_invalid_transition(self):
        ctx = _ctx(tasks_by_issue={1: _task(1, status_labels=(StatusLabel.QUEUED,))})
        result = ctx.record_transition(
            1,
            expected_labels=(StatusLabel.QUEUED,),
            verified_labels=(StatusLabel.QUEUED, StatusLabel.BLOCKED),
            execution_active=False,
        )
        assert result.status == RecordStatus.CONFLICT
        assert result.reason == REASON_INVALID_TRANSITION

    def test_zero_primary_labels_in_verified_is_invalid_transition(self):
        ctx = _ctx(tasks_by_issue={1: _task(1, status_labels=(StatusLabel.QUEUED,))})
        result = ctx.record_transition(
            1,
            expected_labels=(StatusLabel.QUEUED,),
            verified_labels=("priority:high",),
            execution_active=False,
        )
        assert result.status == RecordStatus.CONFLICT
        assert result.reason == REASON_INVALID_TRANSITION

    def test_execution_active_true_targeting_queued_is_execution_mismatch(self):
        ctx = _ctx(
            tasks_by_issue={1: _task(1, status_labels=(StatusLabel.IN_PROGRESS,))},
            run_state=RunState(active_worktrees={"1": _active(1)}),
        )
        result = ctx.record_transition(
            1,
            expected_labels=(StatusLabel.IN_PROGRESS,),
            verified_labels=(StatusLabel.QUEUED,),
            execution_active=True,
        )
        assert result.status == RecordStatus.CONFLICT
        assert result.reason == REASON_EXECUTION_MISMATCH

    def test_blocked_to_in_progress_is_allowed_given_an_existing_launch_fact(self):
        # QUEUED / BLOCKED いずれからもIN_PROGRESSへ遷移できる(遷移表)。
        ctx = _ctx(
            tasks_by_issue={1: _task(1, status_labels=(StatusLabel.BLOCKED,))},
            run_state=RunState(active_worktrees={"1": _active(1)}),
        )
        result = ctx.record_transition(
            1,
            expected_labels=(StatusLabel.BLOCKED,),
            verified_labels=(StatusLabel.IN_PROGRESS,),
            execution_active=True,
        )
        assert result.status == RecordStatus.APPLIED

    def test_human_review_cannot_transition_directly_to_in_progress(self):
        # 人手判断待ちからは同一主状態への再記録のみで、自動でIN_PROGRESSへも
        # 解除しない。
        ctx = _ctx(
            tasks_by_issue={
                1: _task(1, status_labels=(StatusLabel.BLOCKED_HUMAN_REVIEW,))
            },
            run_state=RunState(active_worktrees={"1": _active(1)}),
        )
        result = ctx.record_transition(
            1,
            expected_labels=(StatusLabel.BLOCKED_HUMAN_REVIEW,),
            verified_labels=(StatusLabel.IN_PROGRESS,),
            execution_active=True,
        )
        assert result.status == RecordStatus.CONFLICT
        assert result.reason == REASON_INVALID_TRANSITION

    def test_conflict_does_not_change_any_query_result(self):
        ctx = _ctx(tasks_by_issue={1: _task(1, status_labels=(StatusLabel.DONE,))})
        before_task = ctx.task(1)
        before_done = ctx.is_effectively_done(1)

        result = ctx.record_transition(
            1,
            expected_labels=(StatusLabel.DONE,),
            verified_labels=(StatusLabel.QUEUED,),
            execution_active=False,
        )

        assert result.status == RecordStatus.CONFLICT
        assert ctx.task(1) == before_task
        assert ctx.is_effectively_done(1) == before_done
