"""#660: 完了履歴からの所要時間・トークン・手戻りリスク推定のテスト。"""

import pytest

from orchestune.dispatch.cost_model import (
    DEFAULT_DURATION_SECONDS,
    ESTIMATE_SOURCE_DEFAULT,
    ESTIMATE_SOURCE_FLEET,
    ESTIMATE_SOURCE_TASK,
    build_cost_model,
)
from orchestune.dispatch.state import CompletedWorktree, RunState
from orchestune.models import Task, Usage


def _task(issue_number, subtask_id="task-a"):
    return Task(
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


def _completed(issue_number, started_at, completed_at, total_tokens=None):
    usage = (
        Usage(input_tokens=0, output_tokens=0, total_tokens=total_tokens)
        if total_tokens is not None
        else None
    )
    return CompletedWorktree(
        issue_number=issue_number,
        subtask_id=f"task-{issue_number}",
        branch=f"b{issue_number}",
        started_at=started_at,
        completed_at=completed_at,
        usage=usage,
    )


class TestDurationEstimates:
    def test_empty_history_falls_back_to_the_deterministic_default(self):
        model = build_cost_model(RunState())

        estimate = model.estimate(_task(1))

        assert estimate.duration_seconds == DEFAULT_DURATION_SECONDS
        assert estimate.tokens is None
        assert estimate.rework_risk == 0.0
        assert estimate.source == ESTIMATE_SOURCE_DEFAULT

    def test_task_history_takes_precedence_over_fleet_history(self):
        state = RunState(
            completed_worktrees=[
                _completed(1, 0.0, 100.0),
                _completed(2, 0.0, 900.0),
                _completed(2, 0.0, 900.0),
            ]
        )

        model = build_cost_model(state)

        assert model.estimate(_task(1)).duration_seconds == 100.0
        assert model.estimate(_task(1)).source == ESTIMATE_SOURCE_TASK

    def test_unseen_task_uses_the_fleet_median(self):
        state = RunState(
            completed_worktrees=[
                _completed(1, 0.0, 100.0),
                _completed(2, 0.0, 200.0),
                _completed(3, 0.0, 900.0),
            ]
        )

        estimate = build_cost_model(state).estimate(_task(99))

        assert estimate.duration_seconds == 200.0
        assert estimate.source == ESTIMATE_SOURCE_FLEET

    def test_missing_or_nonsensical_durations_are_discarded(self):
        state = RunState(
            completed_worktrees=[
                _completed(1, None, 100.0),
                _completed(1, 500.0, 100.0),  # completed before it started
            ]
        )

        estimate = build_cost_model(state).estimate(_task(1))

        assert estimate.duration_seconds == DEFAULT_DURATION_SECONDS

    def test_median_is_used_rather_than_the_mean(self):
        state = RunState(
            completed_worktrees=[
                _completed(1, 0.0, 10.0),
                _completed(1, 0.0, 20.0),
                _completed(1, 0.0, 6000.0),
            ]
        )

        assert build_cost_model(state).estimate(_task(1)).duration_seconds == 20.0


class TestTokenEstimates:
    def test_token_estimate_comes_from_recorded_usage(self):
        state = RunState(
            completed_worktrees=[_completed(1, 0.0, 10.0, total_tokens=1234)]
        )

        assert build_cost_model(state).estimate(_task(1)).tokens == 1234

    def test_unknown_tokens_stay_none_when_no_usage_was_ever_recorded(self):
        state = RunState(completed_worktrees=[_completed(1, 0.0, 10.0)])

        assert build_cost_model(state).estimate(_task(1)).tokens is None

    def test_fleet_token_median_is_used_for_unseen_tasks(self):
        state = RunState(
            completed_worktrees=[
                _completed(1, 0.0, 10.0, total_tokens=100),
                _completed(2, 0.0, 10.0, total_tokens=300),
                _completed(3, 0.0, 10.0, total_tokens=200),
            ]
        )

        assert build_cost_model(state).estimate(_task(99)).tokens == 200

    def test_negative_token_counts_are_discarded(self):
        state = RunState(
            completed_worktrees=[_completed(1, 0.0, 10.0, total_tokens=-5)]
        )

        assert build_cost_model(state).estimate(_task(1)).tokens is None


class TestReworkRisk:
    def test_risk_grows_with_the_number_of_prior_attempts(self):
        state = RunState(
            completed_worktrees=[
                _completed(1, 0.0, 10.0),
                _completed(2, 0.0, 10.0),
                _completed(2, 20.0, 30.0),
                _completed(2, 40.0, 50.0),
            ]
        )
        model = build_cost_model(state)

        assert model.estimate(_task(1)).rework_risk == pytest.approx(0.5)
        assert model.estimate(_task(2)).rework_risk == pytest.approx(0.75)
        assert model.estimate(_task(3)).rework_risk == 0.0

    def test_risk_never_reaches_one(self):
        state = RunState(
            completed_worktrees=[_completed(1, 0.0, 10.0) for _ in range(50)]
        )

        assert build_cost_model(state).estimate(_task(1)).rework_risk < 1.0
