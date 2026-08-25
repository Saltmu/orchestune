"""#660: クリティカルパスとリソース制約を考慮したスケジューリングのテスト。"""

import pytest

from orchestune.dag.models import ConflictEdge, ConflictGraph
from orchestune.dispatch.scoring import (
    MIN_PRIORITY_GAP,
    QUALITY_SPAN,
    SCHEDULING_MODE_CRITICAL_PATH,
    SCHEDULING_MODE_LEGACY,
    decision_to_dict,
    reconcile_decisions_with_launches,
    remaining_token_budget,
    select_next_tasks,
    select_tasks_with_decisions,
)
from orchestune.dispatch.state import ActiveWorktree, CompletedWorktree, RunState
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
    yaml_error=False,
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
        yaml_error=yaml_error,
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


class TestPriorityOrderingInvariant:
    """PR#665レビュー指摘(Codex P2)の回帰防止。

    critical path/cost由来の項は、待ち時間が等しい候補同士でpriorityの順序を
    逆転させてはならない。逆転は「片方がbonus満額・もう片方がpenalty満額」で
    起きるため、判定すべきは片側のbonus合計ではなく候補**間**の差である。
    """

    def test_quality_terms_cannot_span_one_priority_step(self):
        assert QUALITY_SPAN < MIN_PRIORITY_GAP

    def test_cheap_high_fan_out_low_priority_loses_to_expensive_medium(self):
        # low側: 最長チェーン・最多後続・トークンほぼ0・試行1回
        hub = _task(1, subtask_id="hub", priority="low")
        # medium側: 後続なし・トークン最大・試行3回（＝penalty満額）
        costly = _task(2, subtask_id="costly", priority="medium")
        known = [
            hub,
            costly,
            _task(10, subtask_id="d1", depends_on=("hub",)),
            _task(11, subtask_id="d2", depends_on=("d1",)),
            _task(12, subtask_id="d3", depends_on=("d2",)),
        ]
        state = RunState(
            completed_worktrees=[
                _completed(1, NOW - 300, total_tokens=1),
                _completed(2, NOW - 300, total_tokens=100_000),
                _completed(2, NOW - 300, total_tokens=100_000),
                _completed(2, NOW - 300, total_tokens=100_000),
            ]
        )

        result = _select(
            [hub, costly], run_state=state, max_concurrent=1, known_tasks=known
        )

        assert result.selected == [costly]


class TestLaunchReconciliation:
    """PR#665レビュー指摘(Codex P2)の回帰防止。

    選出（scheduling）と実起動（launch）は別物であり、レポートは実際に起動された
    タスクだけを`✅ 起動`として扱わなければならない。
    """

    def _decisions(self):
        return _select([_task(1), _task(2)]).decisions

    def test_tasks_that_failed_to_launch_are_marked_launch_failed(self):
        decisions = self._decisions()
        assert [d.selected for d in decisions] == [True, True]

        reconciled = reconcile_decisions_with_launches(decisions, [_task(1)])

        assert reconciled[0].selected is True
        assert reconciled[0].reason == "selected"
        assert reconciled[1].selected is False
        assert reconciled[1].reason == "launch-failed"

    def test_already_skipped_decisions_keep_their_original_reason(self):
        conflicts = ConflictGraph(
            (ConflictEdge("task-1", "task-2", reason="similarity"),)
        )
        decisions = _select([_task(1), _task(2)], conflict_graph=conflicts).decisions

        reconciled = reconcile_decisions_with_launches(decisions, [_task(1)])

        assert [(d.selected, d.reason) for d in reconciled] == [
            (True, "selected"),
            (False, "conflict"),
        ]

    def test_a_fully_successful_launch_leaves_every_decision_untouched(self):
        decisions = self._decisions()

        assert (
            reconcile_decisions_with_launches(decisions, [_task(1), _task(2)])
            == decisions
        )


class TestUnknownTokenCostInvariant:
    """PR#665レビュー指摘(Claude)の回帰防止。

    「トークン量が不明な候補だけがpenalty 0で得をする」状態が起こらないこと。
    `CostModel`はfleet全体の中央値へ縮退するため、`tokens is None`は候補集合の
    全員で同時にしか成立しない——この前提が崩れると不明であることが有利に働く。
    """

    def test_a_task_without_its_own_usage_history_still_gets_a_fleet_estimate(self):
        # issue 1にだけusage記録があり、issue 2には無い状態でも「片方だけ不明」に
        # はならない（issue 2はfleet中央値を受け取る）。
        state = RunState(
            completed_worktrees=[_completed(1, NOW - 300, total_tokens=500)]
        )

        result = _select([_task(1), _task(2)], run_state=state)

        assert [d.estimated_tokens for d in result.decisions] == [500, 500]

    def test_when_no_usage_was_ever_recorded_every_token_penalty_is_zero(self):
        state = RunState(completed_worktrees=[_completed(1, NOW - 300)])

        result = _select([_task(1), _task(2)], run_state=state)

        assert all(d.estimated_tokens is None for d in result.decisions)
        assert all(d.components.token_penalty == 0.0 for d in result.decisions)

    def test_unknown_token_cost_is_never_mixed_with_known_ones(self):
        for state in (
            RunState(),
            RunState(completed_worktrees=[_completed(1, NOW - 300)]),
            RunState(completed_worktrees=[_completed(1, NOW - 300, total_tokens=900)]),
        ):
            decisions = _select(
                [_task(1), _task(2), _task(3)], run_state=state
            ).decisions
            unknown = [d.estimated_tokens is None for d in decisions]
            assert all(unknown) or not any(unknown)


class TestIneligibleCandidatesAreStillReported:
    """PR#665レビュー指摘(Codex P2)の回帰防止。

    スコアリング以前に候補から外れたタスクも、理由付きの未選出判定として
    レポートへ残さなければならない（特に`yaml_error`のタスクはapply時に
    実際に処理されるため、レポートから消えると運用者が追えなくなる）。
    """

    def test_yaml_error_candidate_is_reported_with_its_reason(self):
        broken = _task(2, yaml_error=True)

        result = _select([_task(1), broken])

        assert [t.issue_number for t in result.selected] == [1]
        excluded = next(d for d in result.decisions if d.issue_number == 2)
        assert excluded.selected is False
        assert excluded.reason == "yaml-error"
        assert excluded.score == 0.0

    def test_externally_locked_candidate_is_reported(self):
        locked = _task(2, status_labels=("status:queued", "status:external-lock"))

        result = _select([_task(1), locked])

        assert result.selected == [_task(1)]
        assert (
            next(d for d in result.decisions if d.issue_number == 2).reason
            == "external-lock"
        )

    def test_blocked_recompute_candidate_is_reported(self):
        blocked = _task(2, status_labels=("status:queued", "status:blocked-recompute"))

        result = _select([_task(1), blocked])

        assert (
            next(d for d in result.decisions if d.issue_number == 2).reason
            == "blocked-recompute"
        )

    def test_already_active_candidate_is_reported(self):
        state = RunState(
            active_worktrees={
                "2": ActiveWorktree(2, "b", "w", 1, NOW - 600, ()),
            }
        )

        result = _select([_task(1), _task(2)], run_state=state)

        assert [t.issue_number for t in result.selected] == [1]
        assert (
            next(d for d in result.decisions if d.issue_number == 2).reason
            == "already-active"
        )

    def test_every_candidate_including_ineligible_ones_appears_exactly_once(self):
        candidates = [
            _task(1),
            _task(2, yaml_error=True),
            _task(3, status_labels=("status:queued", "status:external-lock")),
            _task(4),
        ]

        result = _select(candidates, max_concurrent=1)

        assert sorted(d.issue_number for d in result.decisions) == [1, 2, 3, 4]
        assert {d.reason for d in result.decisions} == {
            "selected",
            "yaml-error",
            "external-lock",
            "quota-exhausted",
        }

    def test_ineligible_candidates_do_not_consume_quota_slots(self):
        candidates = [
            _task(1, yaml_error=True),
            _task(2, yaml_error=True),
            _task(3),
            _task(4),
        ]

        result = _select(candidates, max_concurrent=2)

        assert [t.issue_number for t in result.selected] == [3, 4]

    def test_ineligible_decisions_carry_the_configured_mode(self):
        result = _select(
            [_task(1, yaml_error=True)], scheduling_mode=SCHEDULING_MODE_LEGACY
        )

        assert result.decisions[0].mode == SCHEDULING_MODE_LEGACY
