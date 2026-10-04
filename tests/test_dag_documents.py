"""共有ドキュメントのfootprint完全一致による競合検出(#724)のテスト。"""

import re

import pytest

from orchestune.dag.documents import (
    build_shared_document_conflicts,
    declares_shared_document,
    is_shared_document_path,
)
from orchestune.dag.graph import build_dag
from orchestune.dag.models import SubTask


def _subtask(id_, footprint, symbols=(), shared_contract=None, writes=False):
    return SubTask(
        id=id_,
        description="",
        footprint=tuple(footprint),
        symbols=tuple(symbols),
        depends_on=(),
        risk=False,
        risk_reasons=(),
        shared_contract=shared_contract,
        writes_shared_contract=writes,
    )


class TestIsSharedDocumentPath:
    @pytest.mark.parametrize(
        "path",
        [
            "docs/README.md",
            "docs/ja/architecture.md",
            "docs/en/architecture/integration.md",
            "docs/a/b/c/d/e.md",
            "./docs//ja/usage.md",
            "docs\\ja\\usage.md",
        ],
    )
    def test_detects_markdown_under_root_docs(self, path):
        assert is_shared_document_path(path)

    @pytest.mark.parametrize(
        "path",
        [
            "README.md",
            "skills/orchestune/SKILL.md",
            "packages/a/docs/guide.md",
            "docs/guide.rst",
            "docs/guide.MD",
            "Docs/guide.md",
            "docs/guide.md.bak",
            "docs/.md",
            "documentation/guide.md",
            "orchestune/dag/documents.py",
        ],
    )
    def test_ignores_other_paths(self, path):
        assert not is_shared_document_path(path)

    def test_invalid_path_is_not_a_document(self):
        assert not is_shared_document_path("/docs/a.md")
        assert not is_shared_document_path("../docs/a.md")


class TestDeclaresSharedDocument:
    def test_true_when_any_footprint_is_a_document(self):
        assert declares_shared_document(_subtask("a", ["x.py", "docs/a.md"]))

    def test_false_for_code_only_footprint(self):
        assert not declares_shared_document(_subtask("a", ["x.py"]))


class TestBuildSharedDocumentConflicts:
    def test_same_document_creates_symmetric_edge(self):
        edges = build_shared_document_conflicts(
            [_subtask("b", ["docs/ja/a.md"]), _subtask("a", ["docs/ja/a.md"])]
        )
        assert len(edges) == 1
        edge = edges[0]
        assert (edge.left, edge.right) == ("a", "b")
        assert edge.reason == "shared-document"
        assert edge.score is None
        assert edge.resources == ("docs/ja/a.md",)

    def test_all_pairs_for_three_tasks(self):
        edges = build_shared_document_conflicts(
            [_subtask(i, ["docs/a.md"]) for i in ("a", "b", "c")]
        )
        assert {e.pair for e in edges} == {
            frozenset(("a", "b")),
            frozenset(("a", "c")),
            frozenset(("b", "c")),
        }

    def test_different_documents_in_same_directory_do_not_conflict(self):
        assert not build_shared_document_conflicts(
            [_subtask("a", ["docs/ja/a.md"]), _subtask("b", ["docs/ja/b.md"])]
        )

    def test_ja_and_en_documents_are_distinct(self):
        assert not build_shared_document_conflicts(
            [
                _subtask("a", ["docs/ja/architecture.md"]),
                _subtask("b", ["docs/en/architecture.md"]),
            ]
        )

    def test_normalized_paths_match(self):
        edges = build_shared_document_conflicts(
            [
                _subtask("a", ["docs/ja/a.md"]),
                _subtask("b", ["./docs//ja/a.md"]),
                _subtask("c", ["docs\\ja\\a.md"]),
            ]
        )
        assert len(edges) == 3
        assert all(e.resources == ("docs/ja/a.md",) for e in edges)

    def test_duplicate_path_within_one_task_is_collapsed(self):
        edges = build_shared_document_conflicts(
            [
                _subtask("a", ["docs/a.md", "./docs/a.md"]),
                _subtask("b", ["docs/a.md"]),
            ]
        )
        assert len(edges) == 1
        assert edges[0].resources == ("docs/a.md",)

    def test_multiple_shared_documents_produce_one_edge(self):
        edges = build_shared_document_conflicts(
            [
                _subtask("a", ["docs/b.md", "docs/a.md"]),
                _subtask("b", ["docs/a.md", "docs/b.md"]),
            ]
        )
        assert len(edges) == 1
        assert edges[0].resources == ("docs/a.md", "docs/b.md")

    def test_result_is_independent_of_input_order(self):
        tasks = [
            _subtask("a", ["docs/x.md", "docs/y.md"]),
            _subtask("b", ["docs/y.md"]),
            _subtask("c", ["docs/x.md"]),
        ]
        assert build_shared_document_conflicts(tasks) == (
            build_shared_document_conflicts(list(reversed(tasks)))
        )

    def test_non_document_shared_paths_are_not_detected(self):
        assert not build_shared_document_conflicts(
            [_subtask("a", ["x.py", "README.md"]), _subtask("b", ["x.py", "README.md"])]
        )

    def test_ignore_patterns_remove_matching_document(self):
        tasks = [_subtask("a", ["docs/a.md"]), _subtask("b", ["docs/a.md"])]
        assert not build_shared_document_conflicts(tasks, [re.compile(r"^docs/a\.md$")])

    def test_ignore_patterns_accept_generator_and_keep_other_documents(self):
        tasks = [
            _subtask("a", ["docs/a.md", "docs/b.md"]),
            _subtask("b", ["docs/a.md", "docs/b.md"]),
        ]
        edges = build_shared_document_conflicts(
            tasks, (p for p in [re.compile(r"a\.md$")])
        )
        assert len(edges) == 1
        assert edges[0].resources == ("docs/b.md",)

    def test_symbols_do_not_imply_documents(self):
        assert not build_shared_document_conflicts(
            [
                _subtask("a", ["x.py"], symbols=["docs/a.md"]),
                _subtask("b", ["y.py"], symbols=["docs/a.md"]),
            ]
        )


class TestBuildDagIntegration:
    """Issue #724 reproduction: similarity ~0.125 (< 0.2) hid the shared document."""

    def _tasks(self, **extra):
        return [
            SubTask(
                id=f"t{i}",
                description="",
                footprint=(f"src/m{i}a.py", f"src/m{i}b.py", "docs/ja/architecture.md"),
                symbols=(f"sym{i}",),
                depends_on=(),
                risk=False,
                risk_reasons=(),
                **extra,
            )
            for i in range(3)
        ]

    def test_shared_document_edges_ignore_similarity_threshold(self):
        dag = build_dag(self._tasks())
        document_pairs = {
            edge.pair
            for edge in dag.conflict_graph.edges
            if edge.reason == "shared-document"
        }
        assert document_pairs == {
            frozenset(("t0", "t1")),
            frozenset(("t0", "t2")),
            frozenset(("t1", "t2")),
        }

    def test_precedence_dag_is_unchanged(self):
        dag = build_dag(self._tasks())
        assert dag.edges == []
        assert dag.parallel_leaves == ["t0", "t1", "t2"]

    def test_explicit_dependency_keeps_document_edge(self):
        tasks = self._tasks()
        tasks[1] = SubTask(**{**tasks[1].__dict__, "depends_on": ("t0",)})
        dag = build_dag(tasks)
        assert dag.conflict_graph.has_conflict("t0", "t1")
        assert [(e.source, e.target) for e in dag.edges] == [("t0", "t1")]
