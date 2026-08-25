"""#660: クリティカルパスとリソース制約を考慮したスケジューリングのテスト。"""

import pytest

from orchestune.dag.models import ConflictEdge, ConflictGraph
from orchestune.dispatch.scoring import (
    SCHEDULING_MODE_CRITICAL_PATH,
    SCHEDULING_MODE_LEGACY,
    decision_to_dict,
    remaining_token_budget,
    select_next_tasks,
    select_tasks_with_decisions,
)
from orchestune.dispatch.state import CompletedWorktree, RunState
from orchestune.models import Task, Usage

NOW = 1_800_000_000.0
CREATED_AT = "2026-01-01T00:00:00+00:00"


def _task(
    issue_number,
    subtask_id=None,
    priority="medium",
    depends_on=(),
    created_at=CREATED_AT,
    status_labels=("status:queued",),
):
    return Task(
        issue_number=issue_number,
        subtask_id=subtask_id or f"task-{issue_number}",
        footprint=(),
        symbols=(),
        risk=False,
        priority=priority,
        progress_partial=False,
        status_labels=status_labels,
        created_at=created_at,
        depends_on=depends_on,
    )


def _completed(issue_number, completed_at, total_tokens=None, duration=300.0):
    usage = (
        Usage(input_tokens=0, output_tokens=0, total_tokens=total_tokens)
        if total_tokens is not None
        else None
    )
    return CompletedWorktree(
        issue_number=issue_number,
        subtask_id=f"task-{issue_number}",
        branch=f"b{issue_number}",
        started_at=completed_at - duration,
        completed_at=completed_at,
        usage=usage,
    )


def _select(candidates, **kwargs):
    kwargs.setdefault("run_state", RunState())
    kwargs.setdefault("now", NOW)
    kwargs.setdefault("max_concurrent", 5)
    kwargs.setdefault("max_launches_per_window", 5)
    kwargs.setdefault("window_seconds", 3600)
    return select_tasks_with_decisions(candidates, **kwargs)


class TestCriticalPathPriority:
    def test_task_releasing_more_successors_wins_over_an_equal_priority_leaf(self):
        # 同priority・同時刻作成。issue番号のタイブレークではleafが勝つが、
        # 後続を2件解放するcontractの方がcritical path上の価値が高い。
        contract = _task(9, subtask_id="contract")
        leaf = _task(1, subtask_id="leaf")
        downstream = [
            _task(20, subtask_id="d1", depends_on=("contract",)),
            _task(21, subtask_id="d2", depends_on=("contract",)),
        ]

        result = _select(
            [leaf, contract],
            max_concurrent=1,
            known_tasks=[leaf, contract, *downstream],
        )

        assert result.selected == [contract]

    def test_legacy_mode_restores_the_previous_ordering(self):
        contract = _task(9, subtask_id="contract")
        leaf = _task(1, subtask_id="leaf")
        downstream = [_task(20, subtask_id="d1", depends_on=("contract",))]

        result = _select(
            [leaf, contract],
            max_concurrent=1,
            known_tasks=[leaf, contract, *downstream],
            scheduling_mode=SCHEDULING_MODE_LEGACY,
        )

        assert result.selected == [leaf]
        assert all(d.mode == SCHEDULING_MODE_LEGACY for d in result.decisions)

    def test_priority_still_outranks_critical_path_position(self):
        # critical pathはpriorityを置き換えるものではなく、同順位の中の
        # 決め手として働く（bonusは正規化され[0,1]に収まる）。
        leaf = _task(1, subtask_id="leaf", priority="high")
        contract = _task(2, subtask_id="contract")
        downstream = [
            _task(20 + i, subtask_id=f"d{i}", depends_on=("contract",))
            for i in range(5)
        ]

        result = _select(
            [leaf, contract],
            max_concurrent=1,
            known_tasks=[leaf, contract, *downstream],
        )

        assert result.selected == [leaf]

    def test_longer_downstream_chain_wins_over_a_wider_shallow_one(self):
        deep = _task(1, subtask_id="deep")
        wide = _task(2, subtask_id="wide")
        known = [
            deep,
            wide,
            _task(10, subtask_id="deep1", depends_on=("deep",)),
            _task(11, subtask_id="deep2", depends_on=("deep1",)),
            _task(12, subtask_id="deep3", depends_on=("deep2",)),
            _task(13, subtask_id="wide1", depends_on=("wide",)),
        ]

        result = _select([wide, deep], max_concurrent=1, known_tasks=known)

        assert result.selected == [deep]

    def test_finished_downstream_tasks_do_not_inflate_the_rank(self):
        contract = _task(9, subtask_id="contract")
        leaf = _task(1, subtask_id="leaf")
        done_downstream = _task(
            20,
            subtask_id="d1",
            depends_on=("contract",),
            status_labels=("status:done",),
        )

        result = _select(
            [leaf, contract],
            max_concurrent=1,
            known_tasks=[leaf, contract, done_downstream],
        )

        assert result.selected == [leaf]


class TestResourceConstraints:
    def test_launch_window_ceiling_is_never_exceeded(self):
        candidates = [_task(i) for i in range(1, 6)]

        result = _select(candidates, max_launches_per_window=1)

        assert len(result.selected) == 1
        skipped = [d for d in result.decisions if not d.selected]
        assert {d.reason for d in skipped} == {"quota-exhausted"}

    def test_max_concurrent_ceiling_counts_active_worktrees(self):
        candidates = [_task(i) for i in range(1, 6)]

        result = _select(candidates, max_concurrent=2)

        assert len(result.selected) == 2

    def test_estimated_token_cost_bounds_a_batch_within_the_window(self):
        state = RunState(
            completed_worktrees=[_completed(1, NOW - 300, total_tokens=400)]
        )

        result = _select(
            [_task(2), _task(3)],
            run_state=state,
            max_tokens_per_window=1000,
        )

        assert [t.issue_number for t in result.selected] == [2]
        skipped = next(d for d in result.decisions if d.issue_number == 3)
        assert skipped.reason == "token-budget"
        assert skipped.estimated_tokens == 400

    def test_the_first_selection_is_never_blocked_by_the_token_budget(self):
        # 単体で上限を超える見積りのタスクしか無い場合でも、キューが
        # 永久に進まなくならないよう先頭1件は必ず起動できる。
        state = RunState(
            completed_worktrees=[_completed(1, NOW - 300, total_tokens=400)]
        )

        result = _select(
            [_task(2), _task(3)],
            run_state=state,
            max_tokens_per_window=500,
        )

        assert len(result.selected) == 1

    def test_hard_window_cap_still_stops_all_launches(self):
        state = RunState(
            completed_worktrees=[_completed(1, NOW - 300, total_tokens=1000)]
        )

        result = _select([_task(2)], run_state=state, max_tokens_per_window=1000)

        assert result.selected == []

    def test_unknown_token_costs_degrade_to_no_filtering(self):
        state = RunState(completed_worktrees=[_completed(1, NOW - 300)])

        result = _select(
            [_task(2), _task(3)], run_state=state, max_tokens_per_window=10
        )

        assert len(result.selected) == 2
        assert all(d.estimated_tokens is None for d in result.decisions)

    def test_no_token_ceiling_means_no_token_budget(self):
        assert remaining_token_budget(RunState(), NOW, 3600, None) is None

    def test_remaining_budget_ignores_usage_outside_the_window(self):
        state = RunState(
            completed_worktrees=[_completed(1, NOW - 7200, total_tokens=900)]
        )

        assert remaining_token_budget(state, NOW, 3600, 1000) == 1000

    def test_conflicting_tasks_never_share_a_batch(self):
        conflicts = ConflictGraph(
            (ConflictEdge("task-1", "task-2", reason="similarity"),)
        )

        result = _select([_task(1), _task(2), _task(3)], conflict_graph=conflicts)

        assert [t.issue_number for t in result.selected] == [1, 3]
        blocked = next(d for d in result.decisions if d.issue_number == 2)
        assert blocked.reason == "conflict"

    def test_expensive_tasks_are_penalised_relative_to_cheap_ones(self):
        state = RunState(
            completed_worktrees=[
                _completed(1, NOW - 300, total_tokens=100_000),
                _completed(2, NOW - 300, total_tokens=100),
            ]
        )

        result = _select([_task(1), _task(2)], run_state=state, max_concurrent=1)

        assert [t.issue_number for t in result.selected] == [2]

    def test_repeatedly_reworked_tasks_are_penalised(self):
        state = RunState(
            completed_worktrees=[
                _completed(1, NOW - 300),
                _completed(1, NOW - 300),
                _completed(1, NOW - 300),
                _completed(2, NOW - 300),
            ]
        )

        result = _select([_task(1), _task(2)], run_state=state, max_concurrent=1)

        assert [t.issue_number for t in result.selected] == [2]


class TestFairness:
    def test_every_continuously_eligible_task_is_eventually_selected(self):
        # 「有限個の継続的にeligibleなタスクは、resourceが供給され続ける限り
        # 最終的に必ず選択される」——agingが非有界なため、bounded な
        # critical-path/価値項に勝つ時点が必ず訪れる。
        candidates = [
            _task(1, subtask_id="hub"),
            _task(2, subtask_id="plain-a"),
            _task(3, subtask_id="plain-b"),
            _task(4, subtask_id="plain-c", priority="low"),
        ]
        known = candidates + [
            _task(10 + i, subtask_id=f"d{i}", depends_on=("hub",)) for i in range(6)
        ]
        state = RunState()
        seen: set[str] = set()

        now = NOW
        for _ in range(60):
            result = _select(
                candidates,
                run_state=state,
                now=now,
                max_concurrent=1,
                max_launches_per_window=1,
                known_tasks=known,
            )
            assert result.selected, "resourceが供給されているのに何も選ばれなかった"
            task = result.selected[0]
            seen.add(task.subtask_id)
            now += 600.0
            state.completed_worktrees.append(_completed(task.issue_number, now))

        assert seen == {t.subtask_id for t in candidates}

    def test_a_starved_task_outscores_a_freshly_completed_high_value_one(self):
        hub = _task(1, subtask_id="hub")
        starved = _task(2, subtask_id="starved")
        known = [hub, starved] + [
            _task(10 + i, subtask_id=f"d{i}", depends_on=("hub",)) for i in range(4)
        ]
        state = RunState(completed_worktrees=[_completed(1, NOW - 60)])

        result = _select(
            [hub, starved], run_state=state, max_concurrent=1, known_tasks=known
        )

        assert result.selected == [starved]


class TestDeterminism:
    def test_identical_inputs_produce_identical_decisions(self):
        candidates = [_task(i) for i in range(1, 8)]
        known = candidates + [_task(30, subtask_id="d", depends_on=("task-3",))]
        state = RunState(completed_worktrees=[_completed(2, NOW - 900, 500)])

        first = _select(
            candidates, run_state=state, max_concurrent=3, known_tasks=known
        )
        second = _select(
            list(reversed(candidates)),
            run_state=state,
            max_concurrent=3,
            known_tasks=list(reversed(known)),
        )

        assert first.selected == second.selected
        assert sorted(first.decisions, key=lambda d: d.issue_number) == sorted(
            second.decisions, key=lambda d: d.issue_number
        )

    def test_large_candidate_sets_stay_within_the_search_bound(self):
        candidates = [_task(1)] + [
            _task(i, depends_on=(f"task-{i - 1}",)) for i in range(2, 601)
        ]

        result = _select(candidates, max_concurrent=2, known_tasks=candidates)

        assert len(result.selected) == 2
        assert len(result.decisions) == len(candidates)
        assert (
            result.selected
            == _select(candidates, max_concurrent=2, known_tasks=candidates).selected
        )


class TestObservability:
    def test_every_candidate_is_reported_exactly_once_with_a_reason(self):
        conflicts = ConflictGraph(
            (ConflictEdge("task-1", "task-2", reason="similarity"),)
        )
        candidates = [_task(i) for i in range(1, 5)]

        result = _select(candidates, max_concurrent=2, conflict_graph=conflicts)

        assert [d.issue_number for d in result.decisions] == [1, 2, 3, 4]
        assert {d.reason for d in result.decisions} <= {
            "selected",
            "conflict",
            "quota-exhausted",
            "token-budget",
        }

    def test_components_add_up_to_the_reported_score(self):
        known = [_task(1), _task(2), _task(10, subtask_id="d", depends_on=("task-1",))]

        result = _select([_task(1), _task(2)], known_tasks=known)

        for decision in result.decisions:
            assert decision.score == pytest.approx(decision.components.total)
            assert decision.mode == SCHEDULING_MODE_CRITICAL_PATH

    def test_decision_serialises_rank_and_estimated_cost(self):
        state = RunState(
            completed_worktrees=[_completed(1, NOW - 300, total_tokens=700)]
        )

        result = _select([_task(1)], run_state=state)

        payload = decision_to_dict(result.decisions[0])
        assert payload["subtask_id"] == "task-1"
        assert payload["bottom_level"] > 0
        assert payload["estimated_tokens"] == 700
        assert payload["estimated_duration_seconds"] == 300.0
        assert payload["reason"] == "selected"
        assert payload["components"]["critical_path"] >= 0.0

    def test_select_next_tasks_keeps_returning_a_plain_task_list(self):
        assert select_next_tasks(
            [_task(1)],
            RunState(),
            now=NOW,
            max_concurrent=1,
            max_launches_per_window=1,
            window_seconds=3600,
        ) == [_task(1)]
