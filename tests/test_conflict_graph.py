"""Precedence DAG and symmetric conflict graph separation (#659)."""

import pytest

from orchestune.dag.graph import build_dag, recompute_dag_for_footprint_change
from orchestune.dag.models import ConflictEdge, ConflictGraph, DagCycleError, SubTask
from tests.dag_test_support import _subtask


def test_conflict_edge_is_canonical_and_symmetric():
    edge = ConflictEdge(
        left="task-b",
        right="task-a",
        reason="similarity",
        score=0.75,
        resources=("symbol.shared", "src/shared.py"),
    )
    graph = ConflictGraph((edge,))

    assert (edge.left, edge.right) == ("task-a", "task-b")
    assert graph.has_conflict("task-a", "task-b")
    assert graph.has_conflict("task-b", "task-a")
    assert graph.neighbors("task-a") == frozenset({"task-b"})


def test_similarity_is_a_conflict_but_not_a_precedence_edge():
    dag = build_dag(
        [
            _subtask("task-a", ["src/shared.py"], ["shared.symbol"]),
            _subtask("task-b", ["src/shared.py"], ["shared.symbol"]),
        ],
        threshold=0.2,
    )

    assert dag.edges == []
    assert dag.topological_order == ["task-a", "task-b"]
    assert dag.parallel_leaves == ["task-a", "task-b"]
    assert len(dag.conflict_graph.edges) == 1
    conflict = dag.conflict_graph.edges[0]
    assert (conflict.left, conflict.right) == ("task-a", "task-b")
    assert conflict.reason == "similarity"
    assert conflict.score is not None
    assert set(conflict.resources) == {"src/shared.py", "shared.symbol"}


def test_dependency_without_overlap_has_no_conflict():
    dag = build_dag(
        [
            _subtask("task-a", ["src/a.py"], []),
            _subtask("task-b", ["src/b.py"], [], depends_on=("task-a",)),
        ]
    )

    assert [(edge.source, edge.target) for edge in dag.edges] == [("task-a", "task-b")]
    assert dag.conflict_graph.edges == ()


def test_dependency_and_conflict_can_coexist_without_forming_a_cycle():
    dag = build_dag(
        [
            _subtask("task-a", ["src/shared.py"], []),
            _subtask("task-b", ["src/shared.py"], [], depends_on=("task-a",)),
        ],
        threshold=0.1,
    )

    assert [(edge.source, edge.target) for edge in dag.edges] == [("task-a", "task-b")]
    assert dag.conflict_graph.has_conflict("task-a", "task-b")
    assert dag.topological_order == ["task-a", "task-b"]


def test_only_precedence_edges_participate_in_cycle_detection():
    dag = build_dag(
        [
            _subtask("task-a", ["src/shared.py"], [], depends_on=("task-c",)),
            _subtask("task-b", ["src/shared.py"], []),
            _subtask("task-c", ["src/c.py"], []),
        ],
        threshold=0.1,
    )
    assert dag.topological_order.index("task-c") < dag.topological_order.index("task-a")
    assert dag.conflict_graph.has_conflict("task-a", "task-b")

    with pytest.raises(DagCycleError):
        build_dag(
            [
                _subtask("task-a", [], [], depends_on=("task-b",)),
                _subtask("task-b", [], [], depends_on=("task-a",)),
            ]
        )


def test_shared_contract_writers_conflict_but_consumers_do_not():
    writer_a = SubTask(
        "writer-a",
        "",
        ("src/a/custom.py",),
        (),
        (),
        False,
        (),
        shared_contract="plugin-registry",
        writes_shared_contract=True,
    )
    writer_b = SubTask(
        "writer-b",
        "",
        ("src/b/custom.py",),
        (),
        (),
        False,
        (),
        shared_contract="plugin-registry",
        writes_shared_contract=True,
    )
    consumer = SubTask(
        "consumer",
        "",
        ("src/consumer.py",),
        (),
        ("writer-a",),
        False,
        (),
        shared_contract="plugin-registry",
    )

    dag = build_dag([writer_a, writer_b, consumer], threshold=0.9)

    assert dag.conflict_graph.has_conflict("writer-a", "writer-b")
    assert not dag.conflict_graph.has_conflict("writer-a", "consumer")
    contract_edge = next(
        edge for edge in dag.conflict_graph.edges if edge.reason == "shared-contract"
    )
    assert contract_edge.resources == ("shared_contract:plugin-registry",)


def test_runtime_footprint_change_adds_conflict_not_precedence():
    subtasks = {
        "task-a": _subtask("task-a", ["src/a.py"], []),
        "task-b": _subtask("task-b", ["src/b.py"], []),
    }

    after, events = recompute_dag_for_footprint_change(
        subtasks,
        "task-a",
        updated_footprint=("src/a.py", "src/b.py"),
        threshold=0.1,
    )

    assert after.edges == []
    assert after.conflict_graph.has_conflict("task-a", "task-b")
    assert len(events) == 1
    assert events[0].reason == "similarity"
    assert events[0].resources == ("src/b.py",)


def test_json_separates_precedence_and_conflict_edges():
    dag = build_dag(
        [
            _subtask("task-a", ["src/shared.py"], []),
            _subtask("task-b", ["src/shared.py"], [], depends_on=("task-a",)),
        ],
        threshold=0.1,
    ).to_dict()

    assert dag["edges"] == dag["precedence_edges"]
    assert dag["precedence_edges"][0]["reason"] == "explicit"
    assert dag["conflict_edges"][0]["reason"] == "similarity"
    assert set(dag["conflict_edges"][0]["resources"]) == {"src/shared.py"}
