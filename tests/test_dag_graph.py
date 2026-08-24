"""DAG構築・循環解消・footprint再計算・footprintパス正規化のテスト。

test_dag.py (1050行) からグラフ構築関連(#479)を分割。フロントマターパースは
test_dag_parsing.py、類似度計算は test_dag_similarity.py を参照。
"""

import json
import re
import textwrap

import pytest

from orchestune.dag.graph import (
    build_dag,
    build_dag_from_plan,
    recompute_dag_for_footprint_change,
)
from orchestune.dag.models import (
    DagCycleError,
    SubTask,
    compile_extra_ignore_patterns,
    normalize_footprint_path,
)
from orchestune.dag.parsing import parse_decomposition_plan
from tests.dag_test_support import _subtask, _write_plan


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

    def test_threshold_changes_similarity_edge_outcome(self):
        subtasks = [
            _subtask("a", ["src/x.py", "src/1.py", "src/2.py", "src/3.py"], []),
            _subtask("b", ["src/x.py", "src/9.py", "src/8.py", "src/7.py"], []),
        ]
        dag_low_threshold = build_dag(subtasks, threshold=0.1)
        dag_high_threshold = build_dag(subtasks, threshold=0.9)
        assert len(dag_low_threshold.edges) == 1
        assert len(dag_high_threshold.edges) == 0

    def test_ignore_patterns_propagate_through_build_dag(self):
        subtasks = [
            _subtask("a", ["package.json", "src/a.py"], []),
            _subtask("b", ["package.json", "src/b.py"], []),
        ]
        extra_patterns = compile_extra_ignore_patterns([r"(^|/)package\.json$"])
        dag = build_dag(subtasks, threshold=0.1, ignore_patterns=extra_patterns)
        assert dag.edges == []

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

    def test_ignore_patterns_generator_applies_to_both_internal_calls(self):
        # recompute_dag_for_footprint_changeは内部でbuild_similarity_edgesを
        # 2回呼ぶため、ジェネレータを渡しても2回目が空にならず、tuple指定時と
        # 同じ結果になること
        subtasks = {
            "a": _subtask("a", ["package.json"], []),
            "b": _subtask("b", ["package.json"], []),
        }
        pattern = re.compile(r"(^|/)package\.json$")

        def _pattern_generator():
            yield pattern

        after, conflicts = recompute_dag_for_footprint_change(
            subtasks,
            subtask_id="a",
            updated_footprint=["package.json", "src/only_a.py"],
            updated_symbols=[],
            threshold=0.1,
            ignore_patterns=_pattern_generator(),
        )
        assert conflicts == []
        assert after.edges == []


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

    def test_resolves_cycle_warning_is_added_to_dag_result(self):
        # Issue #395: 循環解消で捨てられたエッジの警告が DagResult.warnings に反映されること
        subtasks = [
            SubTask("a", "", ("src/shared.py",), (), (), False, (), "medium"),
            SubTask("b", "", ("src/shared.py",), (), ("a",), False, (), "high"),
            SubTask("c", "", ("src/shared.py",), (), (), False, (), "high"),
        ]
        # c -> a (similarity 1.0) is the weakest because priority of 'a' is medium, 'c' is high. Wait, direction is c->a ?
        # Actually let's use the exact same subtasks as the previous test to reliably trigger the warning.
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
        dag = build_dag(subtasks, threshold=0.1)

        # Check that the warning is included in DagResult.warnings
        assert any(
            "循環参照を検出したため、類似度エッジを自動解消しました: c -> a" in w
            for w in dag.warnings
        )

    def test_does_not_resolve_explicit_only_cycles(self):
        # 明示的な依存関係のみで循環が発生した場合、自動解消できずに例外を投げること
        subtasks = [
            _subtask("a", [], [], depends_on=["b"]),
            _subtask("b", [], [], depends_on=["a"]),
        ]
        with pytest.raises(DagCycleError):
            build_dag(subtasks)


class TestNormalizeFootprintPath:
    def test_normalizes_relative_posix_paths(self):
        assert normalize_footprint_path("src/core.py") == "src/core.py"
        assert normalize_footprint_path("./src/core.py") == "src/core.py"
        assert normalize_footprint_path("./src//core.py") == "src/core.py"
        assert normalize_footprint_path("src\\core.py") == "src/core.py"
        assert normalize_footprint_path("src/foo/../core.py") == "src/core.py"

    def test_raises_for_absolute_and_drive_qualified_paths(self):
        with pytest.raises(ValueError, match="absolute path"):
            normalize_footprint_path("/src/core.py")

        with pytest.raises(ValueError, match="drive-qualified"):
            normalize_footprint_path("C:\\src\\core.py")

        with pytest.raises(ValueError, match="drive-qualified"):
            normalize_footprint_path("C:/src/core.py")

    def test_raises_for_escaping_repository_root(self):
        with pytest.raises(ValueError, match="escapes repository root"):
            normalize_footprint_path("../core.py")

        with pytest.raises(ValueError, match="escapes repository root"):
            normalize_footprint_path("src/../../core.py")

    def test_raises_for_empty_or_root_path(self):
        with pytest.raises(ValueError, match="resolves to empty or root"):
            normalize_footprint_path("")

        with pytest.raises(ValueError, match="resolves to empty or root"):
            normalize_footprint_path(".")


def test_subtask_post_init_normalizes_footprint():
    subtask = SubTask(
        id="task-1",
        description="Test subtask",
        footprint=("./src/core.py", "src\\utils.py"),
        symbols=(),
        depends_on=(),
        risk=False,
        risk_reasons=(),
    )
    assert subtask.footprint == ("src/core.py", "src/utils.py")
    assert subtask.touch_set() == frozenset({"src/core.py", "src/utils.py"})


def test_touch_set_ignore_patterns_generator_behaves_like_equivalent_tuple():
    # is_ignored_footprintは footprint の各パスに対して複数回呼ばれるため、
    # ジェネレータのような使い捨てiterableを渡しても、同じ内容のtupleを
    # 渡した場合と結果が一致すること
    subtask = _subtask("a", ["src/a.py", "package.json"], [])
    pattern = re.compile(r"(^|/)package\.json$")

    def _pattern_generator():
        yield pattern

    assert subtask.touch_set(_pattern_generator()) == subtask.touch_set((pattern,))
    assert subtask.touch_set((pattern,)) == frozenset({"src/a.py"})


def test_dag_conflict_edge_generated_for_unnormalized_equivalent_paths(tmp_path):
    plan_content = """\
    ---
    subtasks:
      - id: task-a
        description: "Task A"
        footprint:
          - ./src/core.py
        depends_on: []
      - id: task-b
        description: "Task B"
        footprint:
          - src/core.py
        depends_on: []
    ---
    """
    plan_path = _write_plan(tmp_path, plan_content)
    subtasks = parse_decomposition_plan(plan_path)

    res = build_dag(subtasks)

    # Footprints should be normalized to src/core.py
    assert res.subtasks["task-a"].footprint == ("src/core.py",)
    assert res.subtasks["task-b"].footprint == ("src/core.py",)

    # A similarity edge must exist between task-a and task-b due to shared normalized footprint
    assert len(res.edges) == 1
    edge = res.edges[0]
    assert edge.reason == "similarity"
    assert {edge.source, edge.target} == {"task-a", "task-b"}
