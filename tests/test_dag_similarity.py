"""類似度スコア計算・候補ペア絞り込み・類似度エッジ生成のテスト。

test_dag.py (1050行) から類似度関連(#479)を分割。フロントマターパースは
test_dag_parsing.py、グラフ構築は test_dag_graph.py を参照。
"""

import re

import pytest

from orchestune.dag.models import compile_extra_ignore_patterns
from orchestune.dag.similarity import build_similarity_edges
from orchestune.dag.similarity import find_candidate_pairs as _find_candidate_pairs
from orchestune.dag.similarity import otsuka_ochiai as _otsuka_ochiai
from tests.dag_test_support import _subtask


class TestSimilarityScore:
    def test_otsuka_ochiai_identical_sets(self):
        s = frozenset({"a", "b"})
        assert _otsuka_ochiai(s, s) == pytest.approx(1.0)

    def test_otsuka_ochiai_disjoint_sets(self):
        assert _otsuka_ochiai(frozenset({"a"}), frozenset({"b"})) == 0.0

    def test_otsuka_ochiai_empty_set_is_zero(self):
        assert _otsuka_ochiai(frozenset(), frozenset({"a"})) == 0.0

    def test_otsuka_ochiai_partial_overlap(self):
        # |intersection|=2, sqrt(3*3)=3 -> 2/3
        s1 = frozenset({"a", "b", "c"})
        s2 = frozenset({"a", "b", "d"})
        assert _otsuka_ochiai(s1, s2) == pytest.approx(2 / 3)


class TestCandidatePairPrefilter:
    def test_shares_at_least_one_item_becomes_candidate(self):
        subtasks = [
            _subtask("a", ["src/x.py"], ["x.foo"]),
            _subtask("b", ["src/x.py"], ["x.bar"]),
        ]
        pairs = _find_candidate_pairs(subtasks)
        assert ("a", "b") in pairs

    def test_disjoint_subtasks_are_not_candidates(self):
        subtasks = [
            _subtask("a", ["src/x.py"], ["x.foo"]),
            _subtask("b", ["src/y.py"], ["y.bar"]),
        ]
        pairs = _find_candidate_pairs(subtasks)
        assert pairs == set()


class TestBuildSimilarityEdges:
    def test_edge_created_above_threshold(self):
        subtasks = [
            _subtask("a", ["src/x.py"], ["x.foo", "x.bar"]),
            _subtask("b", ["src/x.py"], ["x.foo", "x.baz"]),
        ]
        edges = build_similarity_edges(subtasks, threshold=0.2)
        assert len(edges) == 1
        assert {edges[0].source, edges[0].target} == {"a", "b"}
        assert edges[0].reason == "similarity"

    def test_no_edge_below_threshold(self):
        subtasks = [
            _subtask("a", ["src/x.py", "src/1.py", "src/2.py", "src/3.py"], []),
            _subtask("b", ["src/x.py", "src/9.py", "src/8.py", "src/7.py"], []),
        ]
        edges = build_similarity_edges(subtasks, threshold=0.9)
        assert edges == []

    def test_ignore_patterns_generator_behaves_like_equivalent_tuple(self):
        # ignore_patternsは内部で複数回反復されるため、ジェネレータのような
        # 使い捨てiterableを渡しても、同じ内容のtupleを渡した場合と結果が一致すること
        subtasks = [
            _subtask("a", ["package.json", "src/a.py"], []),
            _subtask("b", ["package.json", "src/b.py"], []),
        ]
        pattern = re.compile(r"(^|/)package\.json$")

        def _pattern_generator():
            yield pattern

        edges_from_generator = build_similarity_edges(
            subtasks, threshold=0.1, ignore_patterns=_pattern_generator()
        )
        edges_from_tuple = build_similarity_edges(
            subtasks, threshold=0.1, ignore_patterns=(pattern,)
        )
        assert edges_from_generator == edges_from_tuple == []


class TestWeightedSimilarity:
    def test_file_overlap_has_higher_score_than_symbol_overlap(self):
        # ファイルが1つ重なり、シンボルは異なるペア
        subtasks_file = [
            _subtask("a", ["src/x.py"], ["symbol1"]),
            _subtask("b", ["src/x.py"], ["symbol2"]),
        ]
        # シンボルが1つ重なり、ファイルは異なるペア
        subtasks_symbol = [
            _subtask("a", ["src/x.py"], ["symbol_shared"]),
            _subtask("b", ["src/y.py"], ["symbol_shared"]),
        ]

        edges_file = build_similarity_edges(subtasks_file, threshold=0.0)
        edges_symbol = build_similarity_edges(subtasks_symbol, threshold=0.0)

        assert len(edges_file) == 1
        assert len(edges_symbol) == 1
        # ファイル重なりの方がスコア（類似度）が高くなるはず
        assert edges_file[0].score > edges_symbol[0].score


class TestGlobalIgnorePatterns:
    def test_ignored_files_do_not_cause_similarity_edges(self):
        # pyproject.toml だけが共通しているケース
        subtasks = [
            _subtask("a", ["pyproject.toml", "src/a.py"], []),
            _subtask("b", ["pyproject.toml", "src/b.py"], []),
        ]
        # 無視されていれば、実質共通ファイルがないため類似度エッジは張られないはず
        edges = build_similarity_edges(subtasks, threshold=0.1)
        assert len(edges) == 0

    def test_default_patterns_still_apply_when_no_extra_patterns_given(self):
        # 既定パターンのみで実行した場合は、従来の挙動から変化がないことを保証する
        subtasks = [
            _subtask("a", ["pyproject.toml", "src/a.py"], []),
            _subtask("b", ["pyproject.toml", "src/b.py"], []),
        ]
        edges = build_similarity_edges(subtasks, threshold=0.1, ignore_patterns=())
        assert len(edges) == 0

    def test_extra_ignore_pattern_excludes_non_python_manifest(self):
        # package.json はPython向けの既定パターンには含まれないため、
        # 追加パターンなしでは衝突検知の対象になってしまう
        subtasks = [
            _subtask("a", ["package.json", "src/a.py"], []),
            _subtask("b", ["package.json", "src/b.py"], []),
        ]
        edges_without_extra = build_similarity_edges(subtasks, threshold=0.1)
        assert len(edges_without_extra) == 1

        extra_patterns = compile_extra_ignore_patterns([r"(^|/)package\.json$"])
        edges_with_extra = build_similarity_edges(
            subtasks, threshold=0.1, ignore_patterns=extra_patterns
        )
        assert len(edges_with_extra) == 0

    def test_extra_ignore_patterns_are_additive_to_defaults(self):
        # 追加パターンを指定しても、既定のPython向けパターンは維持される
        subtasks = [
            _subtask("a", ["pyproject.toml", "package.json"], []),
            _subtask("b", ["pyproject.toml", "package.json"], []),
        ]
        extra_patterns = compile_extra_ignore_patterns([r"(^|/)package\.json$"])
        edges = build_similarity_edges(
            subtasks, threshold=0.1, ignore_patterns=extra_patterns
        )
        assert len(edges) == 0
