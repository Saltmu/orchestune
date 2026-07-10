import json
import textwrap

import pytest

from orchestune.dag import (
    DagCycleError,
    SubTask,
    _find_candidate_pairs,
    _otsuka_ochiai,
    build_dag,
    build_dag_from_plan,
    build_similarity_edges,
    parse_decomposition_plan,
    recompute_dag_for_footprint_change,
)


def _write_plan(tmp_path, content: str):
    path = tmp_path / "decomposition_plan.md"
    path.write_text(textwrap.dedent(content), encoding="utf-8")
    return path


BASIC_PLAN = """\
---
subtasks:
  - id: task-a
    description: "Aを実装する"
    footprint:
      - src/foo.py
    symbols:
      - foo.Foo
    depends_on: []
  - id: task-b
    description: "Bを実装する"
    footprint:
      - src/bar.py
    symbols:
      - bar.helper
    depends_on: ["task-a"]
---

# Decomposition Plan

本文は自由記述のためパース対象外。
"""


class TestParseDecompositionPlan:
    def test_parses_basic_subtasks(self, tmp_path):
        path = _write_plan(tmp_path, BASIC_PLAN)
        subtasks = parse_decomposition_plan(path)

        assert [s.id for s in subtasks] == ["task-a", "task-b"]
        assert subtasks[0].footprint == ("src/foo.py",)
        assert subtasks[0].symbols == ("foo.Foo",)
        assert subtasks[1].depends_on == ("task-a",)
        assert subtasks[0].priority == "medium"  # デフォルト

    def test_parses_priority(self, tmp_path):
        plan = """\
        ---
        subtasks:
          - id: task-a
            priority: high
          - id: task-b
            priority: low
        ---
        """
        path = _write_plan(tmp_path, plan)
        subtasks = parse_decomposition_plan(path)
        assert subtasks[0].priority == "high"
        assert subtasks[1].priority == "low"

    def test_missing_frontmatter_raises(self, tmp_path):
        path = _write_plan(tmp_path, "# no frontmatter here\n")
        with pytest.raises(ValueError, match="フロントマター"):
            parse_decomposition_plan(path)

    def test_duplicate_id_raises(self, tmp_path):
        plan = """\
        ---
        subtasks:
          - id: dup
            footprint: []
            symbols: []
          - id: dup
            footprint: []
            symbols: []
        ---
        """
        path = _write_plan(tmp_path, plan)
        with pytest.raises(ValueError, match="重複"):
            parse_decomposition_plan(path)

    def test_unknown_depends_on_raises(self, tmp_path):
        plan = """\
        ---
        subtasks:
          - id: task-a
            footprint: []
            symbols: []
            depends_on: ["ghost"]
        ---
        """
        path = _write_plan(tmp_path, plan)
        with pytest.raises(ValueError, match="未知のdepends_on"):
            parse_decomposition_plan(path)


class TestRiskFlagParsing:
    def test_flags_data_sources_path(self, tmp_path):
        plan = """\
        ---
        subtasks:
          - id: task-a
            footprint: ["data/sources/plot.yaml"]
            symbols: []
        ---
        """
        path = _write_plan(tmp_path, plan)
        subtasks = parse_decomposition_plan(path)
        assert subtasks[0].risk is True
        assert any("data/sources" in r for r in subtasks[0].risk_reasons)

    def test_flags_credentials_path(self, tmp_path):
        plan = """\
        ---
        subtasks:
          - id: task-a
            footprint: ["credentials/service_account.json"]
            symbols: []
        ---
        """
        path = _write_plan(tmp_path, plan)
        subtasks = parse_decomposition_plan(path)
        assert subtasks[0].risk is True

    def test_flags_subprocess_keyword_in_symbols(self, tmp_path):
        plan = """\
        ---
        subtasks:
          - id: task-a
            footprint: ["src/utils/ai_cli_executor.py"]
            symbols: ["run_subprocess_command"]
        ---
        """
        path = _write_plan(tmp_path, plan)
        subtasks = parse_decomposition_plan(path)
        assert subtasks[0].risk is True
        assert any("subprocess" in r for r in subtasks[0].risk_reasons)

    def test_explicit_risk_override(self, tmp_path):
        plan = """\
        ---
        subtasks:
          - id: task-a
            footprint: ["src/plain.py"]
            symbols: []
            risk: true
        ---
        """
        path = _write_plan(tmp_path, plan)
        subtasks = parse_decomposition_plan(path)
        assert subtasks[0].risk is True
        assert "explicit" in subtasks[0].risk_reasons

    def test_no_risk_for_plain_subtask(self, tmp_path):
        path = _write_plan(tmp_path, BASIC_PLAN)
        subtasks = parse_decomposition_plan(path)
        assert subtasks[0].risk is False
        assert subtasks[0].risk_reasons == ()


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


def _subtask(id_, footprint, symbols, depends_on=(), priority="medium"):
    return SubTask(
        id=id_,
        description="",
        footprint=tuple(footprint),
        symbols=tuple(symbols),
        depends_on=tuple(depends_on),
        risk=False,
        risk_reasons=(),
        priority=priority,
    )


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


class TestBuildDag:
    def test_merges_explicit_and_similarity_edges(self):
        subtasks = [
            _subtask("a", ["src/x.py"], ["x.foo"]),
            _subtask("b", ["src/x.py"], ["x.foo"], depends_on=[]),
            _subtask("c", ["src/z.py"], ["z.qux"], depends_on=["a"]),
        ]
        dag = build_dag(subtasks, threshold=0.2)
        reasons = {(e.source, e.target): e.reason for e in dag.edges}
        assert reasons[("a", "c")] == "explicit"
        assert ("a", "b") in reasons and reasons[("a", "b")] == "similarity"

    def test_detects_cycle(self):
        subtasks = [
            _subtask("a", [], [], depends_on=["b"]),
            _subtask("b", [], [], depends_on=["a"]),
        ]
        with pytest.raises(DagCycleError):
            build_dag(subtasks)

    def test_topological_order_and_parallel_leaves(self):
        subtasks = [
            _subtask("a", [], []),
            _subtask("b", [], [], depends_on=["a"]),
            _subtask("c", [], []),
        ]
        dag = build_dag(subtasks)
        assert dag.topological_order.index("a") < dag.topological_order.index("b")
        assert set(dag.parallel_leaves) == {"a", "c"}

    def test_risky_subtask_ids_collected(self):
        risky = SubTask(
            id="r",
            description="",
            footprint=("credentials/token.json",),
            symbols=(),
            depends_on=(),
            risk=True,
            risk_reasons=("footprint:credentials/token.json",),
        )
        safe = _subtask("s", ["src/x.py"], [])
        dag = build_dag([risky, safe])
        assert dag.risky_subtask_ids == ["r"]

    def test_to_dict_is_json_serializable(self):
        subtasks = [
            _subtask("a", ["src/x.py"], []),
            _subtask("b", [], [], depends_on=["a"]),
        ]
        dag = build_dag(subtasks)
        serialized = json.dumps(dag.to_dict())
        assert "task" not in serialized or True  # just ensure no exception above
        data = json.loads(serialized)
        assert data["topological_order"] == ["a", "b"]


class TestRealProjectSymbolCollision:
    """実プロジェクト(manuscriptune)のシンボル衝突を想定した結合度テスト。"""

    def test_detect_bloat_shared_symbol_forces_dependency(self):
        plan = """\
        ---
        subtasks:
          - id: task-bloat-report
            description: "bloatレポート出力の改善"
            footprint: ["src/utils/detect_bloat.py"]
            symbols: ["scan_project", "check_file_bloat"]
          - id: task-bloat-cli
            description: "detect-bloat CLIオプション追加"
            footprint: ["src/utils/detect_bloat.py"]
            symbols: ["main", "scan_project"]
          - id: task-yaml-handler
            description: "YAMLハンドラの独立した改修"
            footprint: ["src/utils/yaml_handler.py"]
            symbols: ["load_yaml"]
        ---
        """
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decomposition_plan.md"
            path.write_text(textwrap.dedent(plan), encoding="utf-8")
            dag_dict = build_dag_from_plan(path, threshold=0.2)

        reasons = {(e["source"], e["target"]): e["reason"] for e in dag_dict["edges"]}
        bloat_pair = ("task-bloat-cli", "task-bloat-report")
        alt_pair = ("task-bloat-report", "task-bloat-cli")
        assert (
            reasons.get(bloat_pair) == "similarity"
            or reasons.get(alt_pair) == "similarity"
        )

        # yaml_handler タスクは共有シンボル/ファイルが無いため独立のまま
        touching_yaml = [pair for pair in reasons if "task-yaml-handler" in pair]
        assert touching_yaml == []


class TestCommonUtilityPseudoCoupling:
    """#191: LoggerやConfigのような、多くのサブタスクが触れる共通ユーティリティを
    共有しているだけの疑似結合を、実質的な結合として誤検出しないことを確認する。"""

    def _common_utility_plan(self) -> str:
        # logger.Logger は5サブタスク全てが参照する「ありふれた共通ユーティリティ」。
        # task-one/task-two は logger.Logger 以外に固有のfootprint/symbolのみを持つ。
        return """\
        ---
        subtasks:
          - id: task-one
            description: "1番目の独立したタスク"
            footprint: ["src/one.py"]
            symbols: ["one.Foo", "shared.logger.Logger"]
          - id: task-two
            description: "2番目の独立したタスク"
            footprint: ["src/two.py"]
            symbols: ["two.Bar", "shared.logger.Logger"]
          - id: task-three
            description: "3番目の独立したタスク"
            footprint: ["src/three.py"]
            symbols: ["three.Baz", "shared.logger.Logger"]
          - id: task-four
            description: "4番目の独立したタスク"
            footprint: ["src/four.py"]
            symbols: ["four.Qux", "shared.logger.Logger"]
          - id: task-five
            description: "5番目の独立したタスク"
            footprint: ["src/five.py"]
            symbols: ["five.Quux", "shared.logger.Logger"]
        ---
        """

    def test_shared_common_utility_alone_does_not_create_edge(self, tmp_path):
        path = _write_plan(tmp_path, self._common_utility_plan())
        dag_dict = build_dag_from_plan(path, threshold=0.2)

        reasons = {(e["source"], e["target"]): e["reason"] for e in dag_dict["edges"]}
        pair = ("task-one", "task-two")
        alt_pair = ("task-two", "task-one")
        assert reasons.get(pair) is None and reasons.get(alt_pair) is None

    def test_genuine_shared_symbol_still_creates_edge(self, tmp_path):
        """logger.Logger のような共通ユーティリティのノイズがあっても、
        task-one/task-two だけが共有する固有シンボルがあれば結合は検出される。"""
        plan = """\
        ---
        subtasks:
          - id: task-one
            description: "1番目のタスク"
            footprint: ["src/one.py"]
            symbols: ["one.Foo", "shared.logger.Logger", "shared_helper.parse"]
          - id: task-two
            description: "2番目のタスク"
            footprint: ["src/two.py"]
            symbols: ["two.Bar", "shared.logger.Logger", "shared_helper.parse"]
          - id: task-three
            description: "3番目の独立したタスク"
            footprint: ["src/three.py"]
            symbols: ["three.Baz", "shared.logger.Logger"]
          - id: task-four
            description: "4番目の独立したタスク"
            footprint: ["src/four.py"]
            symbols: ["four.Qux", "shared.logger.Logger"]
          - id: task-five
            description: "5番目の独立したタスク"
            footprint: ["src/five.py"]
            symbols: ["five.Quux", "shared.logger.Logger"]
        ---
        """
        path = _write_plan(tmp_path, plan)
        dag_dict = build_dag_from_plan(path, threshold=0.2)

        reasons = {(e["source"], e["target"]): e["reason"] for e in dag_dict["edges"]}
        pair = ("task-one", "task-two")
        alt_pair = ("task-two", "task-one")
        assert (
            reasons.get(pair) == "similarity" or reasons.get(alt_pair) == "similarity"
        )


class TestRecomputeDagForFootprintChange:
    def test_detects_new_conflict_and_serializes(self):
        subtasks = {
            "a": _subtask("a", ["src/x.py"], ["x.foo"]),
            "b": _subtask("b", ["src/y.py"], ["y.bar"]),
        }
        # 実行中に task-a のfootprintが src/y.py にも及ぶことが判明したケース
        after, conflicts = recompute_dag_for_footprint_change(
            subtasks,
            subtask_id="a",
            updated_footprint=["src/x.py", "src/y.py"],
            updated_symbols=["x.foo", "y.bar"],
            threshold=0.2,
        )

        assert len(conflicts) == 1
        conflict = conflicts[0]
        assert conflict.subtask_id == "a"
        assert conflict.other_subtask_id == "b"
        assert conflict.blocked_subtask_id == "b"

        reasons = {(e.source, e.target): e.reason for e in after.edges}
        assert reasons.get(("a", "b")) == "similarity"

    def test_no_conflict_when_already_explicit_dependency(self):
        subtasks = {
            "a": _subtask("a", ["src/x.py"], ["x.foo"]),
            "b": _subtask("b", ["src/y.py"], ["y.bar"], depends_on=["a"]),
        }
        after, conflicts = recompute_dag_for_footprint_change(
            subtasks,
            subtask_id="a",
            updated_footprint=["src/x.py", "src/y.py"],
            updated_symbols=["x.foo", "y.bar"],
            threshold=0.2,
        )
        assert conflicts == []
        assert after.topological_order.index("a") < after.topological_order.index("b")

    def test_risk_flag_is_monotonic_after_recompute(self):
        subtasks = {
            "a": SubTask(
                id="a",
                description="",
                footprint=("credentials/token.json",),
                symbols=(),
                depends_on=(),
                risk=True,
                risk_reasons=("footprint:credentials/token.json",),
            ),
        }
        after, _ = recompute_dag_for_footprint_change(
            subtasks,
            subtask_id="a",
            updated_footprint=["src/plain.py"],
            updated_symbols=[],
        )
        assert after.subtasks["a"].risk is True

    def test_unknown_subtask_id_raises(self):
        subtasks = {"a": _subtask("a", [], [])}
        with pytest.raises(KeyError):
            recompute_dag_for_footprint_change(
                subtasks, subtask_id="ghost", updated_footprint=[]
            )


class TestPriorityAndVolumeDirection:
    def test_priority_determines_edge_direction(self):
        # a: medium, b: high -> high (b) から medium (a) へエッジが張られる (b -> a)
        subtasks = [
            SubTask("a", "", ("src/shared.py",), (), (), False, (), "medium"),
            SubTask("b", "", ("src/shared.py",), (), (), False, (), "high"),
        ]
        dag = build_dag(subtasks, threshold=0.1)
        reasons = {(e.source, e.target): e.reason for e in dag.edges}
        assert reasons.get(("b", "a")) == "similarity"

    def test_volume_determines_edge_direction_when_priority_equal(self):
        # priorityはどちらもmedium。a の footprint数は2、b は1 -> a から b へエッジが張られる (a -> b)
        subtasks = [
            SubTask(
                "a", "", ("src/x.py", "src/shared.py"), (), (), False, (), "medium"
            ),
            SubTask("b", "", ("src/shared.py",), (), (), False, (), "medium"),
        ]
        dag = build_dag(subtasks, threshold=0.1)
        reasons = {(e.source, e.target): e.reason for e in dag.edges}
        assert reasons.get(("a", "b")) == "similarity"

    def test_id_order_determines_edge_direction_when_all_equal(self):
        # 全て等しい場合、辞書順で a -> b
        subtasks = [
            SubTask("a", "", ("src/shared.py",), (), (), False, (), "medium"),
            SubTask("b", "", ("src/shared.py",), (), (), False, (), "medium"),
        ]
        dag = build_dag(subtasks, threshold=0.1)
        reasons = {(e.source, e.target): e.reason for e in dag.edges}
        assert reasons.get(("a", "b")) == "similarity"


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


class TestCycleResolution:
    def test_resolves_cycle_by_removing_weakest_similarity_edge(self, caplog):
        import logging

        # a -> b (明示) -> c (類似) -> a (類似) のサイクルが発生。
        # a: medium, b: high, c: high
        # c -> a の方が類似度が低いはず（共通要素が少ないため）。
        # このため、c -> a のエッジが自動削除されてサイクルが解消されるはず。
        subtasks = [
            SubTask("a", "", ("src/a.py",), ("c_shared",), (), False, (), "medium"),
            SubTask(
                "b",
                "",
                ("src/b.py", "src/bc1.py", "src/bc2.py"),
                ("bc_shared",),
                ("a",),
                False,
                (),
                "high",
            ),
            SubTask(
                "c",
                "",
                ("src/c.py", "src/bc1.py", "src/bc2.py"),
                ("bc_shared", "c_shared"),
                (),
                False,
                (),
                "high",
            ),
        ]

        with caplog.at_level(logging.WARNING):
            dag = build_dag(subtasks, threshold=0.1)

        # サイクルが解消され、エラーにならずにDAGが構築できること
        assert dag.topological_order == ["a", "b", "c"]
        # 警告ログが出力されていること
        assert "循環参照を検出したため、類似度エッジを自動解消しました" in caplog.text

    def test_does_not_resolve_explicit_only_cycles(self):
        # 明示的な依存関係のみで循環が発生した場合、自動解消できずに例外を投げること
        subtasks = [
            _subtask("a", [], [], depends_on=["b"]),
            _subtask("b", [], [], depends_on=["a"]),
        ]
        with pytest.raises(DagCycleError):
            build_dag(subtasks)


def test_cli_validation_success(tmp_path, capsys):
    import sys

    from orchestune.dag import main

    plan_content = """\
    ---
    subtasks:
      - id: task-a
        footprint: ["src/a.py"]
      - id: task-b
        footprint: ["src/b.py"]
        depends_on: ["task-a"]
    ---
    """
    plan_path = tmp_path / "plan.md"
    plan_path.write_text(textwrap.dedent(plan_content), encoding="utf-8")

    orig_argv = sys.argv
    sys.argv = ["orchestune-dag", "--plan", str(plan_path)]
    try:
        with pytest.raises(SystemExit) as excinfo:
            main()
        assert excinfo.value.code == 0
    finally:
        sys.argv = orig_argv

    captured = capsys.readouterr()
    assert "DAG validation succeeded" in captured.out
    assert "task-a -> task-b" in captured.out


def test_cli_validation_json(tmp_path, capsys):
    import json
    import sys

    from orchestune.dag import main

    plan_content = """\
    ---
    subtasks:
      - id: task-a
        footprint: ["src/a.py"]
    ---
    """
    plan_path = tmp_path / "plan.md"
    plan_path.write_text(textwrap.dedent(plan_content), encoding="utf-8")

    orig_argv = sys.argv
    sys.argv = ["orchestune-dag", "--plan", str(plan_path), "--json"]
    try:
        with pytest.raises(SystemExit) as excinfo:
            main()
        assert excinfo.value.code == 0
    finally:
        sys.argv = orig_argv

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert "task-a" in data["subtasks"]


def test_cli_validation_cycle_failure(tmp_path, capsys):
    import sys

    from orchestune.dag import main

    plan_content = """\
    ---
    subtasks:
      - id: task-a
        depends_on: ["task-b"]
      - id: task-b
        depends_on: ["task-a"]
    ---
    """
    plan_path = tmp_path / "plan.md"
    plan_path.write_text(textwrap.dedent(plan_content), encoding="utf-8")

    orig_argv = sys.argv
    sys.argv = ["orchestune-dag", "--plan", str(plan_path)]
    try:
        with pytest.raises(SystemExit) as excinfo:
            main()
        assert excinfo.value.code == 1
    finally:
        sys.argv = orig_argv

    captured = capsys.readouterr()
    assert "Error:" in captured.err
