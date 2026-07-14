"""グリーンフィールド分解計画向け共有コントラクトゲート(#175)のテスト。"""

import textwrap

from orchestune.dag import DagEdge, SubTask, build_dag, build_dag_from_plan
from orchestune.dag_contracts import (
    _categorize,
    find_unowned_shared_contract_hotspots,
)


def _subtask(id_, footprint, depends_on=()):
    return SubTask(
        id=id_,
        description="",
        footprint=tuple(footprint),
        symbols=(),
        depends_on=tuple(depends_on),
        risk=False,
        risk_reasons=(),
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

    def test_no_match_returns_none(self):
        assert _categorize("src/adapters/csv_adapter.py") is None


class TestFindUnownedSharedContractHotspots:
    def test_disconnected_same_category_warns(self):
        subtasks = [
            _subtask(
                "task-csv", ["src/adapters/csv_adapter.py", "src/formats/registry.py"]
            ),
            _subtask(
                "task-yaml",
                ["src/adapters/yaml_adapter.py", "src/format_registration.py"],
            ),
        ]
        warnings = find_unowned_shared_contract_hotspots(subtasks, edges=[])
        assert len(warnings) == 1
        assert "task-csv" in warnings[0]
        assert "task-yaml" in warnings[0]
        assert "registry" in warnings[0]

    def test_connected_via_explicit_edge_does_not_warn(self):
        subtasks = [
            _subtask("task-csv", ["src/formats/registry.py"]),
            _subtask("task-yaml", ["src/format_registration.py"]),
        ]
        edges = [DagEdge(source="task-csv", target="task-yaml", reason="explicit")]
        warnings = find_unowned_shared_contract_hotspots(subtasks, edges)
        assert warnings == []

    def test_connected_via_similarity_edge_does_not_warn(self):
        subtasks = [
            _subtask("task-csv", ["src/formats/registry.py"]),
            _subtask("task-yaml", ["src/format_registration.py"]),
        ]
        edges = [
            DagEdge(
                source="task-yaml", target="task-csv", reason="similarity", score=0.4
            )
        ]
        warnings = find_unowned_shared_contract_hotspots(subtasks, edges)
        assert warnings == []

    def test_transitively_connected_chain_does_not_warn(self):
        subtasks = [
            _subtask("task-csv", ["src/formats/registry.py"]),
            _subtask("task-shared", []),
            _subtask("task-yaml", ["src/format_registration.py"]),
        ]
        edges = [
            DagEdge(source="task-shared", target="task-csv", reason="explicit"),
            DagEdge(source="task-shared", target="task-yaml", reason="explicit"),
        ]
        warnings = find_unowned_shared_contract_hotspots(subtasks, edges)
        assert warnings == []

    def test_two_categories_each_disconnected_warn_separately(self):
        subtasks = [
            _subtask("task-a", ["src/registry.py", "pyproject.toml"]),
            _subtask("task-b", ["src/registration_helper.py", "package.json"]),
        ]
        warnings = find_unowned_shared_contract_hotspots(subtasks, edges=[])
        assert len(warnings) == 2

    def test_single_subtask_touching_hotspot_does_not_warn(self):
        subtasks = [_subtask("task-solo", ["src/registry.py"])]
        warnings = find_unowned_shared_contract_hotspots(subtasks, edges=[])
        assert warnings == []

    def test_ordinary_footprint_without_hotspot_pattern_does_not_warn(self):
        subtasks = [
            _subtask("task-a", ["src/adapters/csv_adapter.py"]),
            _subtask("task-b", ["src/adapters/yaml_adapter.py"]),
        ]
        warnings = find_unowned_shared_contract_hotspots(subtasks, edges=[])
        assert warnings == []


class TestBuildDagWarningsIntegration:
    def _plan_without_shared_contract_task(self) -> str:
        return """\
        ---
        subtasks:
          - id: task-csv
            description: "CSVアダプターを実装する"
            footprint: ["src/adapters/csv_adapter.py", "src/formats/registry.py"]
          - id: task-yaml
            description: "YAMLアダプターを実装する"
            footprint: ["src/adapters/yaml_adapter.py", "src/format_registration.py"]
        ---
        """

    def test_build_dag_from_plan_surfaces_warning(self, tmp_path):
        path = tmp_path / "decomposition_plan.md"
        path.write_text(
            textwrap.dedent(self._plan_without_shared_contract_task()),
            encoding="utf-8",
        )
        dag_dict = build_dag_from_plan(path, threshold=0.9)

        assert len(dag_dict["warnings"]) == 1
        assert "task-csv" in dag_dict["warnings"][0]
        assert "task-yaml" in dag_dict["warnings"][0]

    def test_shared_contract_task_with_explicit_depends_on_clears_warning(self):
        subtasks = [
            _subtask("task-shared-contract", ["src/formats/registry.py"]),
            _subtask(
                "task-csv",
                ["src/adapters/csv_adapter.py"],
                depends_on=["task-shared-contract"],
            ),
            _subtask(
                "task-yaml",
                ["src/adapters/yaml_adapter.py", "src/format_registration.py"],
                depends_on=["task-shared-contract"],
            ),
        ]
        dag = build_dag(subtasks, threshold=0.9)

        assert dag.warnings == ()
        reasons = {(edge.source, edge.target): edge.reason for edge in dag.edges}
        assert reasons[("task-shared-contract", "task-csv")] == "explicit"
        assert reasons[("task-shared-contract", "task-yaml")] == "explicit"

    def test_warnings_are_json_serializable(self, tmp_path):
        import json

        path = tmp_path / "decomposition_plan.md"
        path.write_text(
            textwrap.dedent(self._plan_without_shared_contract_task()),
            encoding="utf-8",
        )
        dag_dict = build_dag_from_plan(path, threshold=0.9)
        serialized = json.dumps(dag_dict)
        assert "task-csv" in serialized


class TestDagCliWarnings:
    def test_cli_prints_warnings_without_failing(self, tmp_path, capsys):
        import sys

        from orchestune.dag import main

        plan_content = """\
        ---
        subtasks:
          - id: task-csv
            footprint: ["src/adapters/csv_adapter.py", "src/formats/registry.py"]
          - id: task-yaml
            footprint: ["src/adapters/yaml_adapter.py", "src/format_registration.py"]
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
