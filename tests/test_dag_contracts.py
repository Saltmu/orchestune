"""グリーンフィールド分解計画向け共有コントラクトゲート(#175)のテスト。"""

import textwrap

import pytest

from orchestune.dag.contracts import (
    _categorize,
    _scope,
    find_unowned_shared_contract_hotspots,
)
from orchestune.dag.graph import build_dag, build_dag_from_plan
from orchestune.dag.models import DagEdge, SubTask


def _subtask(
    id_, footprint, depends_on=(), shared_contract=None, writes_shared_contract=False
):
    return SubTask(
        id=id_,
        description="",
        footprint=tuple(footprint),
        symbols=(),
        depends_on=tuple(depends_on),
        risk=False,
        risk_reasons=(),
        shared_contract=shared_contract,
        writes_shared_contract=writes_shared_contract,
    )


class TestCategorize:
    def test_registry_pattern(self):
        assert _categorize("src/formats/registry.py") == "registry"
        assert _categorize("src/format_registration.py") == "registry"

    def test_cli_wiring_pattern(self):
        assert _categorize("orchestune/cli.py") == "cli-wiring"
        assert _categorize("src/__main__.py") == "cli-wiring"

    def test_public_api_pattern(self):
        assert _categorize("src/adapters/__init__.py") == "public-api"
        assert _categorize("src/index.ts") == "public-api"

    def test_dependency_manifest_pattern(self):
        assert _categorize("pyproject.toml") == "dependency-manifest"
        assert _categorize("package.json") == "dependency-manifest"
        assert _categorize("uv.lock") == "dependency-manifest"
        assert _categorize("poetry.lock") == "dependency-manifest"

    def test_no_match_returns_none(self):
        assert _categorize("src/adapters/csv_adapter.py") is None


class TestScope:
    def test_scope_is_parent_directory(self):
        assert _scope("packages/auth/__init__.py") == "packages/auth"
        assert _scope("packages/payments/__init__.py") == "packages/payments"

    def test_scope_of_root_file_is_empty(self):
        assert _scope("pyproject.toml") == ""


class TestFindUnownedSharedContractHotspots:
    def test_disconnected_same_scope_warns(self):
        """同一ディレクトリ配下で想定ファイル名だけが異なるケースはヒューリス
        ティック（カテゴリ+scope）で検出できる。"""
        subtasks = [
            _subtask(
                "task-csv",
                ["src/adapters/csv_adapter.py", "src/formats/registry.py"],
            ),
            _subtask(
                "task-yaml",
                ["src/adapters/yaml_adapter.py", "src/formats/registration.py"],
            ),
        ]
        warnings = find_unowned_shared_contract_hotspots(subtasks, edges=[])
        assert len(warnings) == 1
        assert "task-csv" in warnings[0]
        assert "task-yaml" in warnings[0]
        assert "registry" in warnings[0]

    def test_different_scope_is_not_grouped_by_heuristic(self):
        """ディレクトリが異なる別パッケージのpublic-apiは、カテゴリが同じでも
        別ホットスポットとして扱い、誤検知しない。"""
        subtasks = [
            _subtask("task-auth", ["packages/auth/__init__.py"]),
            _subtask("task-payments", ["packages/payments/__init__.py"]),
        ]
        warnings = find_unowned_shared_contract_hotspots(subtasks, edges=[])
        assert warnings == []

    def test_common_ancestor_only_still_warns(self):
        """`shared -> csv`, `shared -> yaml` のように共通の祖先を持つだけの
        2タスクは、互いには到達不能なため並列実行され得る。連結性ではなく
        到達可能性で判定しなければ、この見逃しが発生する（#175レビュー指摘）。"""
        subtasks = [
            _subtask("task-csv", ["src/formats/registry.py"]),
            _subtask("task-shared", []),
            _subtask("task-yaml", ["src/formats/registration.py"]),
        ]
        edges = [
            DagEdge(source="task-shared", target="task-csv", reason="explicit"),
            DagEdge(source="task-shared", target="task-yaml", reason="explicit"),
        ]
        warnings = find_unowned_shared_contract_hotspots(subtasks, edges)
        assert len(warnings) == 1
        assert "task-csv" in warnings[0]
        assert "task-yaml" in warnings[0]

    def test_direct_dependency_between_pair_does_not_warn(self):
        subtasks = [
            _subtask("task-csv", ["src/formats/registry.py"]),
            _subtask("task-yaml", ["src/formats/registration.py"]),
        ]
        edges = [DagEdge(source="task-csv", target="task-yaml", reason="explicit")]
        warnings = find_unowned_shared_contract_hotspots(subtasks, edges)
        assert warnings == []

    def test_chain_of_three_all_ordered_does_not_warn(self):
        """A -> B -> C のように全ペアが祖先/子孫関係にあれば並列実行されない。"""
        subtasks = [
            _subtask("task-a", ["src/formats/registry.py"]),
            _subtask("task-b", ["src/formats/registration.py"]),
            _subtask("task-c", ["src/formats/registrar.py"]),
        ]
        edges = [
            DagEdge(source="task-a", target="task-b", reason="explicit"),
            DagEdge(source="task-b", target="task-c", reason="explicit"),
        ]
        warnings = find_unowned_shared_contract_hotspots(subtasks, edges)
        assert warnings == []

    def test_two_scopes_each_disconnected_warn_separately(self):
        subtasks = [
            _subtask("task-a", ["src/formats/registry.py", "pyproject.toml"]),
            _subtask("task-b", ["src/formats/registration.py", "package.json"]),
        ]
        warnings = find_unowned_shared_contract_hotspots(subtasks, edges=[])
        assert len(warnings) == 2

    def test_single_subtask_touching_hotspot_does_not_warn(self):
        subtasks = [_subtask("task-solo", ["src/registry.py"])]
        warnings = find_unowned_shared_contract_hotspots(subtasks, edges=[])
        assert warnings == []

    def test_same_subtask_multiple_paths_in_same_scope_deduplicated(self):
        """1サブタスクが同一(カテゴリ,scope)に複数パスで触れていても、
        重複エントリを1件に集約する（他のサブタスクとの比較対象は変わらない）。"""
        subtasks = [
            _subtask(
                "task-a",
                ["src/formats/registry.py", "src/formats/registration.py"],
            ),
            _subtask("task-b", ["src/formats/registrar.py"]),
        ]
        warnings = find_unowned_shared_contract_hotspots(subtasks, edges=[])
        assert len(warnings) == 1
        assert warnings[0].count("task-a:") == 1

    def test_ordinary_footprint_without_hotspot_pattern_does_not_warn(self):
        subtasks = [
            _subtask("task-a", ["src/adapters/csv_adapter.py"]),
            _subtask("task-b", ["src/adapters/yaml_adapter.py"]),
        ]
        warnings = find_unowned_shared_contract_hotspots(subtasks, edges=[])
        assert warnings == []


class TestExplicitSharedContractTag:
    def test_same_tag_unordered_pair_warns_even_across_directories(self):
        """明示的なshared_contractタグは、ディレクトリが異なりヒューリス
        ティックでは捕捉できないケース（想定パスのディレクトリごと異なる
        レジストリ）でも検出できる。"""
        subtasks = [
            _subtask(
                "task-csv",
                ["src/adapters/csv_adapter.py", "src/formats/registry.py"],
                shared_contract="format-registry",
            ),
            _subtask(
                "task-yaml",
                ["src/adapters/yaml_adapter.py", "src/format_registration.py"],
                shared_contract="format-registry",
            ),
        ]
        warnings = find_unowned_shared_contract_hotspots(subtasks, edges=[])
        assert len(warnings) == 1
        assert "format-registry" in warnings[0]
        assert "task-csv" in warnings[0]
        assert "task-yaml" in warnings[0]

    def test_pure_consumers_of_shared_contract_do_not_warn(self):
        """所有タスクが共有ファイルを作成し、依存タスクは自身のfootprintに
        その共有ファイルを含めず読み取り・importするだけの場合、消費者同士
        (task-csv, task-yaml)が互いに未接続でも安全に並列実行できるため
        警告しない（#175再レビュー指摘: shared_contractタグは「契約への
        関与」を意味するだけで「書き込み」を意味しない）。"""
        subtasks = [
            _subtask(
                "task-shared-contract",
                ["src/formats/registry.py"],
                shared_contract="format-registry",
            ),
            _subtask(
                "task-csv",
                ["src/adapters/csv_adapter.py"],
                depends_on=["task-shared-contract"],
                shared_contract="format-registry",
            ),
            _subtask(
                "task-yaml",
                ["src/adapters/yaml_adapter.py"],
                depends_on=["task-shared-contract"],
                shared_contract="format-registry",
            ),
        ]
        edges = [
            DagEdge(
                source="task-shared-contract", target="task-csv", reason="explicit"
            ),
            DagEdge(
                source="task-shared-contract", target="task-yaml", reason="explicit"
            ),
        ]
        warnings = find_unowned_shared_contract_hotspots(subtasks, edges)
        assert warnings == []

    def test_tagged_writers_with_common_ancestor_only_still_warn(self):
        """消費者とは異なり、実際に共有ファイルへ書き込む(footprintがカテゴリに
        一致する)サブタスク同士は、共通の祖先を持つだけでは警告が消えない。"""
        subtasks = [
            _subtask("task-shared-contract", [], shared_contract="format-registry"),
            _subtask(
                "task-csv",
                ["src/formats/registry.py"],
                depends_on=["task-shared-contract"],
                shared_contract="format-registry",
            ),
            _subtask(
                "task-yaml",
                ["src/format_registration.py"],
                depends_on=["task-shared-contract"],
                shared_contract="format-registry",
            ),
        ]
        edges = [
            DagEdge(
                source="task-shared-contract", target="task-csv", reason="explicit"
            ),
            DagEdge(
                source="task-shared-contract", target="task-yaml", reason="explicit"
            ),
        ]
        warnings = find_unowned_shared_contract_hotspots(subtasks, edges)
        assert len(warnings) == 1
        assert "task-csv" in warnings[0]
        assert "task-yaml" in warnings[0]

    def test_explicit_writes_flag_overrides_naming_heuristic(self):
        """カテゴリ正規表現に一致しない独自のファイル名でも、
        `writes_shared_contract=True`を明示すれば書き込み者として扱われる。"""
        subtasks = [
            _subtask(
                "task-csv",
                ["src/adapters/csv_adapter.py", "src/formats/plugin_table.py"],
                shared_contract="format-registry",
                writes_shared_contract=True,
            ),
            _subtask(
                "task-yaml",
                ["src/adapters/yaml_adapter.py", "src/other/plugin_map.py"],
                shared_contract="format-registry",
                writes_shared_contract=True,
            ),
        ]
        warnings = find_unowned_shared_contract_hotspots(subtasks, edges=[])
        assert len(warnings) == 1

    def test_different_tags_do_not_interact(self):
        subtasks = [
            _subtask(
                "task-a",
                ["src/a.py"],
                shared_contract="contract-a",
                writes_shared_contract=True,
            ),
            _subtask(
                "task-b",
                ["src/b.py"],
                shared_contract="contract-b",
                writes_shared_contract=True,
            ),
        ]
        warnings = find_unowned_shared_contract_hotspots(subtasks, edges=[])
        assert warnings == []

    def test_tagged_pair_is_not_double_reported_by_heuristic_pass(self):
        """タグ付き同士の重複はexplicit側で1件報告され、同一ペアがヒューリス
        ティック側で重複報告されない（warned_pairsによる抑止）。"""
        subtasks = [
            _subtask(
                "task-csv",
                ["src/formats/registry.py"],
                shared_contract="format-registry",
            ),
            _subtask(
                "task-yaml",
                ["src/formats/registration.py"],
                shared_contract="format-registry",
            ),
        ]
        warnings = find_unowned_shared_contract_hotspots(subtasks, edges=[])
        assert len(warnings) == 1

    def test_missing_tag_on_one_side_still_detected_by_heuristic_pass(self):
        """タグ付けが片方のサブタスクにしか行われなかった場合（宣言漏れ）でも、
        ヒューリスティック段階は全サブタスクを対象とするため、同じ共有ファイル
        への並列書き込みを見逃さない（#175再々レビュー指摘）。"""
        subtasks = [
            _subtask(
                "task-csv",
                ["src/formats/registry.py"],
                shared_contract="format-registry",
            ),
            _subtask(
                "task-yaml",
                ["src/formats/registration.py"],
                # shared_contract の付け忘れを想定
            ),
        ]
        warnings = find_unowned_shared_contract_hotspots(subtasks, edges=[])
        assert len(warnings) == 1
        assert "task-csv" in warnings[0]
        assert "task-yaml" in warnings[0]


class TestBuildDagWarningsIntegration:
    def _plan_with_shared_contract_tag(self) -> str:
        return """\
        ---
        subtasks:
          - id: task-csv
            description: "CSVアダプターを実装する"
            footprint: ["src/adapters/csv_adapter.py", "src/formats/registry.py"]
            shared_contract: format-registry
          - id: task-yaml
            description: "YAMLアダプターを実装する"
            footprint: ["src/adapters/yaml_adapter.py", "src/format_registration.py"]
            shared_contract: format-registry
        ---
        """

    def test_build_dag_from_plan_suppresses_pair_protected_by_explicit_contract(
        self, tmp_path
    ):
        """同じ明示契約のwriterペアはshared-contract排他辺で保護済みのため、
        build_dag経由では警告しない(#724)。"""
        path = tmp_path / "decomposition_plan.md"
        path.write_text(
            textwrap.dedent(self._plan_with_shared_contract_tag()),
            encoding="utf-8",
        )
        dag_dict = build_dag_from_plan(path, threshold=0.9)

        assert dag_dict["warnings"] == []
        assert dag_dict["subtasks"]["task-csv"]["shared_contract"] == "format-registry"

    def test_build_dag_from_plan_surfaces_warning_for_one_sided_tag(self, tmp_path):
        path = tmp_path / "decomposition_plan.md"
        plan = self._plan_with_shared_contract_tag().replace(
            '"src/format_registration.py"]\n            shared_contract: format-registry\n',
            '"src/formats/registration.py"]\n',
        )
        assert "src/formats/registration.py" in plan
        path.write_text(textwrap.dedent(plan), encoding="utf-8")
        dag_dict = build_dag_from_plan(path, threshold=0.9)

        assert len(dag_dict["warnings"]) == 1
        assert "task-csv" in dag_dict["warnings"][0]
        assert "task-yaml" in dag_dict["warnings"][0]

    def test_explicit_owner_task_with_depends_on_still_flags_parallel_siblings(self):
        """所有タスクへdepends_onするだけでは、依存先同士(csv/yaml)が並列の
        ままなら警告は消えない — これは本来検出すべき障害そのものであるため。"""
        subtasks = [
            _subtask(
                "task-shared-contract",
                ["src/formats/registry.py"],
            ),
            _subtask(
                "task-csv",
                ["src/adapters/csv_adapter.py", "src/formats/registration.py"],
                depends_on=["task-shared-contract"],
            ),
            _subtask(
                "task-yaml",
                ["src/adapters/yaml_adapter.py", "src/formats/registrar.py"],
                depends_on=["task-shared-contract"],
            ),
        ]
        dag = build_dag(subtasks, threshold=0.9)

        assert len(dag.warnings) == 1
        assert "task-csv" in dag.warnings[0]
        assert "task-yaml" in dag.warnings[0]

    def test_direct_dependency_between_dependents_clears_warning(self):
        subtasks = [
            _subtask("task-shared-contract", ["src/formats/registry.py"]),
            _subtask(
                "task-csv",
                ["src/adapters/csv_adapter.py", "src/formats/registration.py"],
                depends_on=["task-shared-contract"],
            ),
            _subtask(
                "task-yaml",
                ["src/adapters/yaml_adapter.py", "src/formats/registrar.py"],
                depends_on=["task-shared-contract", "task-csv"],
            ),
        ]
        dag = build_dag(subtasks, threshold=0.9)

        assert dag.warnings == ()
        reasons = {(edge.source, edge.target): edge.reason for edge in dag.edges}
        assert reasons[("task-shared-contract", "task-csv")] == "explicit"
        assert reasons[("task-csv", "task-yaml")] == "explicit"

    def test_warnings_are_json_serializable(self, tmp_path):
        import json

        path = tmp_path / "decomposition_plan.md"
        path.write_text(
            textwrap.dedent(self._plan_with_shared_contract_tag()),
            encoding="utf-8",
        )
        dag_dict = build_dag_from_plan(path, threshold=0.9)
        serialized = json.dumps(dag_dict)
        assert "task-csv" in serialized


class TestDagCliWarnings:
    def test_cli_prints_warnings_without_failing(self, tmp_path, capsys):
        import sys

        from orchestune.dag.cli import main

        plan_content = """\
        ---
        subtasks:
          - id: task-csv
            footprint: ["src/adapters/csv_adapter.py", "src/formats/registry.py"]
            shared_contract: format-registry
          - id: task-yaml
            footprint: ["src/adapters/yaml_adapter.py", "src/format_registration.py"]
            shared_contract: format-registry
        ---
        """
        plan_path = tmp_path / "plan.md"
        plan_path.write_text(textwrap.dedent(plan_content), encoding="utf-8")

        orig_argv = sys.argv
        sys.argv = ["orchestune-dag", "--plan", str(plan_path)]
        try:
            import pytest

            with pytest.raises(SystemExit) as excinfo:
                main()
            assert excinfo.value.code == 0
        finally:
            sys.argv = orig_argv

        captured = capsys.readouterr()
        assert "Warnings:" in captured.out
        assert "task-csv" in captured.out


class TestDocumentWriter:
    """#724: footprintに共有文書を宣言したタスクはwriterとして扱う。"""

    def test_document_declaration_is_writer(self):
        from orchestune.dag.contracts import is_contract_writer

        assert is_contract_writer(_subtask("a", ["docs/ja/usage.md"]))

    def test_read_only_consumer_is_not_writer(self):
        from orchestune.dag.contracts import is_contract_writer

        assert not is_contract_writer(_subtask("a", ["src/x.py"], shared_contract="c"))

    def test_non_target_markdown_is_not_writer(self):
        from orchestune.dag.contracts import is_contract_writer

        assert not is_contract_writer(_subtask("a", ["README.md"]))

    def test_representative_path_prefers_document_over_unrelated_code(self):
        from orchestune.dag.contracts import _representative_path

        subtask = _subtask("a", ["src/x.py", "docs/ja/usage.md"])
        assert _representative_path(subtask) == "docs/ja/usage.md"

    def test_representative_path_keeps_hotspot_priority(self):
        from orchestune.dag.contracts import _representative_path

        subtask = _subtask("a", ["docs/a.md", "src/registry.py"])
        assert _representative_path(subtask) == "src/registry.py"

    def test_documents_in_different_files_with_same_contract_are_writers(self):
        from orchestune.dag.graph import build_conflict_graph

        subtasks = [
            _subtask("a", ["docs/ja/a.md"], shared_contract="c"),
            _subtask("b", ["docs/ja/b.md"], shared_contract="c"),
        ]
        graph = build_conflict_graph(subtasks)
        assert [e.reason for e in graph.edges] == ["shared-contract"]


def _graph(*edges):
    from orchestune.dag.models import ConflictEdge, ConflictGraph

    return ConflictGraph(tuple(ConflictEdge(*edge) for edge in edges))


def _contract_edge(left, right, contract="c"):
    return (left, right, "shared-contract", None, (f"shared_contract:{contract}",))


class TestConflictGraphSuppression:
    def _writers(self, *ids, contract="c"):
        return [_subtask(i, [f"docs/ja/{i}.md"], shared_contract=contract) for i in ids]

    def test_pair_protected_by_matching_explicit_edge_is_suppressed(self):
        subtasks = self._writers("a", "b")
        graph = _graph(_contract_edge("a", "b"))
        assert (
            find_unowned_shared_contract_hotspots(subtasks, [], conflict_graph=graph)
            == []
        )

    def test_without_graph_warning_is_kept(self):
        warnings = find_unowned_shared_contract_hotspots(self._writers("a", "b"), [])
        assert len(warnings) == 1

    @pytest.mark.parametrize(
        "edge",
        [
            ("a", "b", "shared-document", None, ("docs/ja/a.md",)),
            ("a", "b", "similarity", 0.9, ()),
            ("a", "b", "shared-contract-hotspot", None, ("shared_contract_hotspot:x",)),
            _contract_edge("a", "b", contract="other"),
        ],
    )
    def test_other_edges_do_not_suppress_explicit_warning(self, edge):
        warnings = find_unowned_shared_contract_hotspots(
            self._writers("a", "b"), [], conflict_graph=_graph(edge)
        )
        assert len(warnings) == 1

    def test_only_unresolved_pairs_are_listed(self):
        subtasks = self._writers("a", "b", "c")
        graph = _graph(_contract_edge("a", "b"))
        (warning,) = find_unowned_shared_contract_hotspots(
            subtasks, [], conflict_graph=graph
        )
        assert "a×c" in warning
        assert "b×c" in warning
        assert "a×b" not in warning

    def test_dependency_ordered_pair_is_still_not_warned(self):
        edges = [DagEdge(source="a", target="b", reason="explicit")]
        assert (
            find_unowned_shared_contract_hotspots(
                self._writers("a", "b"), edges, conflict_graph=_graph()
            )
            == []
        )

    def test_warning_wording_does_not_claim_parallel_execution(self):
        (warning,) = find_unowned_shared_contract_hotspots(self._writers("a", "b"), [])
        assert "並列実行され得る" not in warning
        assert "未指定" in warning
        assert "確認が必要" in warning


class TestHeuristicSuppression:
    def _pair(self, tag_a="c", tag_b="c"):
        return [
            _subtask("a", ["src/formats/registry.py"], shared_contract=tag_a),
            _subtask("b", ["src/formats/registration.py"], shared_contract=tag_b),
        ]

    def test_same_contract_with_explicit_edge_is_suppressed(self):
        graph = _graph(_contract_edge("a", "b"))
        assert (
            find_unowned_shared_contract_hotspots(
                self._pair(), [], conflict_graph=graph
            )
            == []
        )

    def test_automatic_hotspot_edge_alone_keeps_warning(self):
        graph = _graph(
            (
                "a",
                "b",
                "shared-contract-hotspot",
                None,
                ("shared_contract_hotspot:registry:src/formats",),
            ),
            ("a", "b", "similarity", 0.9, ()),
        )
        warnings = find_unowned_shared_contract_hotspots(
            self._pair(None, None), [], conflict_graph=graph
        )
        assert len(warnings) == 1

    def test_one_sided_tag_keeps_warning(self):
        graph = _graph(_contract_edge("a", "b"))
        warnings = find_unowned_shared_contract_hotspots(
            self._pair("c", None), [], conflict_graph=graph
        )
        assert len(warnings) == 1

    def test_different_tags_keep_warning(self):
        graph = _graph(_contract_edge("a", "b", "c"), _contract_edge("a", "b", "d"))
        warnings = find_unowned_shared_contract_hotspots(
            self._pair("c", "d"), [], conflict_graph=graph
        )
        assert len(warnings) == 1

    def test_mixed_protected_and_unprotected_third_lists_unresolved_only(self):
        subtasks = [
            *self._pair(),
            _subtask("c", ["src/formats/registrar.py"]),
        ]
        graph = _graph(_contract_edge("a", "b"))
        (warning,) = find_unowned_shared_contract_hotspots(
            subtasks, [], conflict_graph=graph
        )
        assert "a×c" in warning
        assert "b×c" in warning
        assert "a×b" not in warning


class TestDocumentDetectionIsNotAWarning:
    def test_shared_document_alone_adds_no_unowned_warning(self):
        dag = build_dag(
            [_subtask("a", ["docs/ja/a.md"]), _subtask("b", ["docs/ja/a.md"])]
        )
        assert dag.warnings == ()
        assert "shared-document" in {e.reason for e in dag.conflict_graph.edges}


class TestBuildDagSuppression:
    def test_explicit_writers_with_document_edges_are_not_warned(self):
        dag = build_dag(
            [
                _subtask("a", ["docs/ja/a.md"], shared_contract="c"),
                _subtask("b", ["docs/ja/b.md"], shared_contract="c"),
            ]
        )
        assert dag.warnings == ()
