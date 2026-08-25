"""#660: Precedence DAG由来のスケジューリングrank（bottom level / 解放数）のテスト。"""

from orchestune.dispatch.critical_path import (
    MAX_TRANSITIVE_CLOSURE_NODES,
    compute_precedence_ranks,
    pending_tasks,
)
from orchestune.models import Task


def _task(subtask_id, depends_on=(), status_labels=("status:queued",), state="OPEN"):
    return Task(
        issue_number=abs(hash(subtask_id)) % 10_000,
        subtask_id=subtask_id,
        footprint=(),
        symbols=(),
        risk=False,
        priority="medium",
        progress_partial=False,
        status_labels=status_labels,
        created_at="2026-01-01T00:00:00+00:00",
        depends_on=depends_on,
        issue_state=state,
    )


class TestBottomLevel:
    def test_chain_ranks_decrease_towards_the_tail(self):
        # a -> b -> c: 先頭ほど残りチェーンが長く、bottom levelが大きい。
        tasks = [_task("a"), _task("b", ("a",)), _task("c", ("b",))]

        ranks = compute_precedence_ranks(tasks, {"a": 1.0, "b": 1.0, "c": 1.0})

        assert ranks.bottom_level_of("a") == 3.0
        assert ranks.bottom_level_of("b") == 2.0
        assert ranks.bottom_level_of("c") == 1.0

    def test_bottom_level_uses_estimated_durations(self):
        tasks = [_task("a"), _task("b", ("a",))]

        ranks = compute_precedence_ranks(tasks, {"a": 10.0, "b": 100.0})

        assert ranks.bottom_level_of("a") == 110.0
        assert ranks.bottom_level_of("b") == 100.0

    def test_fan_out_root_outranks_its_leaves(self):
        tasks = [_task("root"), *(_task(f"leaf{i}", ("root",)) for i in range(3))]

        ranks = compute_precedence_ranks(tasks, dict.fromkeys(["root"], 1.0))

        assert ranks.bottom_level_of("root") > ranks.bottom_level_of("leaf0")
        assert ranks.unlocked_count("root") == 3
        assert ranks.downstream_count("root") == 3
        assert ranks.unlocked_count("leaf0") == 0

    def test_fan_in_counts_each_predecessor_once(self):
        tasks = [_task("a"), _task("b"), _task("sink", ("a", "b"))]

        ranks = compute_precedence_ranks(tasks, {})

        assert ranks.unlocked_count("a") == 1
        assert ranks.unlocked_count("b") == 1
        assert ranks.downstream_count("sink") == 0

    def test_longest_of_multiple_critical_paths_wins(self):
        # root -> short, root -> long1 -> long2 の2経路。長い方が採用される。
        tasks = [
            _task("root"),
            _task("short", ("root",)),
            _task("long1", ("root",)),
            _task("long2", ("long1",)),
        ]

        ranks = compute_precedence_ranks(
            tasks, {"root": 1.0, "short": 1.0, "long1": 1.0, "long2": 1.0}
        )

        assert ranks.bottom_level_of("root") == 3.0
        assert ranks.downstream_count("root") == 3

    def test_diamond_counts_the_shared_descendant_once(self):
        tasks = [
            _task("root"),
            _task("left", ("root",)),
            _task("right", ("root",)),
            _task("join", ("left", "right")),
        ]

        ranks = compute_precedence_ranks(tasks, {})

        assert ranks.downstream_count("root") == 3
        assert ranks.unlocked_count("root") == 2

    def test_unknown_dependency_ids_are_ignored(self):
        # 依存先が既に完了しIssue一覧から消えている場合でもクラッシュしない。
        ranks = compute_precedence_ranks([_task("only", ("already-done",))], {})

        assert ranks.bottom_level_of("only") > 0.0
        assert ranks.downstream_count("only") == 0

    def test_unknown_subtask_id_ranks_zero(self):
        ranks = compute_precedence_ranks([_task("a")], {})

        assert ranks.bottom_level_of("missing") == 0.0
        assert ranks.unlocked_count("missing") == 0
        assert ranks.downstream_count("missing") == 0

    def test_cyclic_dependencies_terminate_deterministically(self):
        # depends_onが循環していてもクラッシュ・無限ループしないこと
        # （Issue本文の手編集などで壊れたメタデータが渡り得る）。
        tasks = [_task("a", ("b",)), _task("b", ("a",))]

        ranks = compute_precedence_ranks(tasks, {"a": 1.0, "b": 1.0})

        assert ranks.bottom_level_of("a") > 0.0
        assert ranks.bottom_level_of("b") > 0.0
        assert compute_precedence_ranks(tasks, {"a": 1.0, "b": 1.0}) == ranks

    def test_self_dependency_is_ignored(self):
        ranks = compute_precedence_ranks([_task("a", ("a",))], {"a": 2.0})

        assert ranks.bottom_level_of("a") == 2.0
        assert ranks.unlocked_count("a") == 0

    def test_duplicate_subtask_ids_do_not_double_count(self):
        tasks = [_task("a"), _task("a"), _task("b", ("a",))]

        ranks = compute_precedence_ranks(tasks, {})

        assert ranks.unlocked_count("a") == 1

    def test_empty_task_list(self):
        ranks = compute_precedence_ranks([], {})

        assert ranks.bottom_level_of("anything") == 0.0
        assert ranks.exact_downstream is True

    def test_ranks_are_deterministic_for_equivalent_inputs(self):
        tasks = [_task("a"), _task("b", ("a",)), _task("c", ("a",))]

        assert compute_precedence_ranks(tasks, {}) == compute_precedence_ranks(
            list(reversed(tasks)), {}
        )


class TestSearchBound:
    def test_large_graphs_degrade_to_direct_successor_counts(self):
        # 上限超過時は推移閉包を打ち切り、直接の後続数へ決定論的に縮退する。
        size = MAX_TRANSITIVE_CLOSURE_NODES + 2
        tasks = [_task("n0")] + [_task(f"n{i}", (f"n{i - 1}",)) for i in range(1, size)]

        ranks = compute_precedence_ranks(tasks, {})

        assert ranks.exact_downstream is False
        assert ranks.downstream_count("n0") == 1
        # bottom levelは打ち切りの対象外（O(V+E)なので常に厳密）。
        assert ranks.bottom_level_of("n0") > ranks.bottom_level_of("n1")

    def test_small_graphs_compute_exact_transitive_closure(self):
        tasks = [_task("n0"), _task("n1", ("n0",)), _task("n2", ("n1",))]

        ranks = compute_precedence_ranks(tasks, {})

        assert ranks.exact_downstream is True
        assert ranks.downstream_count("n0") == 2


class TestPendingTasks:
    def test_done_and_closed_tasks_are_excluded(self):
        queued = _task("queued")
        done = _task("done", status_labels=("status:done",))
        not_needed = _task("skip", status_labels=("status:not-needed",))
        closed = _task("closed", state="CLOSED")

        assert pending_tasks([queued, done, not_needed, closed]) == [queued]

    def test_tasks_without_a_subtask_id_are_excluded(self):
        assert pending_tasks([_task("")]) == []


class TestDefensiveInputs:
    def test_tasks_without_a_subtask_id_are_skipped_by_the_graph_builder(self):
        # `pending_tasks`を通さずに直接呼ばれても、ID無しのタスクで落ちないこと。
        ranks = compute_precedence_ranks([_task(""), _task("a")], {})

        assert ranks.bottom_level_of("a") > 0.0
        assert ranks.bottom_level_of("") == 0.0

    def test_nonsensical_durations_fall_back_to_the_unit_default(self):
        tasks = [_task("a"), _task("b"), _task("c")]

        ranks = compute_precedence_ranks(
            tasks, {"a": float("nan"), "b": -5.0, "c": 0.0}
        )

        assert ranks.bottom_level_of("a") == 1.0
        assert ranks.bottom_level_of("b") == 1.0
        assert ranks.bottom_level_of("c") == 1.0
