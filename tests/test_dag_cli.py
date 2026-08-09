"""CLI/config-file behavior for `orchestune-dag` (issue #398)."""

import json
import sys
import textwrap

import pytest

from orchestune.dag_cli import main
from orchestune.dag_models import extract_dag_ignore_patterns, load_orchestune_config


def _write_plan(path, content: str) -> None:
    path.write_text(textwrap.dedent(content), encoding="utf-8")


def _run_cli(argv, expected_exit_code=0):
    orig_argv = sys.argv
    sys.argv = ["orchestune-dag", *argv]
    try:
        with pytest.raises(SystemExit) as excinfo:
            main()
        assert excinfo.value.code == expected_exit_code
    finally:
        sys.argv = orig_argv


def _run_cli_json(argv, capsys):
    """Run the CLI with --json and return the parsed DAG result dict."""
    _run_cli([*argv, "--json"])
    return json.loads(capsys.readouterr().out)


def test_cli_validation_success(tmp_path, capsys):
    plan_path = tmp_path / "plan.md"
    _write_plan(
        plan_path,
        """\
        ---
        subtasks:
          - id: task-a
            footprint: ["src/a.py"]
          - id: task-b
            footprint: ["src/b.py"]
            depends_on: ["task-a"]
        ---
        """,
    )

    _run_cli(["--plan", str(plan_path)])

    captured = capsys.readouterr()
    assert "DAG validation succeeded" in captured.out
    assert "task-a -> task-b" in captured.out


def test_cli_validation_json(tmp_path, capsys):
    plan_path = tmp_path / "plan.md"
    _write_plan(
        plan_path,
        """\
        ---
        subtasks:
          - id: task-a
            footprint: ["src/a.py"]
        ---
        """,
    )

    _run_cli(["--plan", str(plan_path), "--json"])

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert "task-a" in data["subtasks"]


def test_cli_validation_cycle_failure(tmp_path, capsys):
    plan_path = tmp_path / "plan.md"
    _write_plan(
        plan_path,
        """\
        ---
        subtasks:
          - id: task-a
            depends_on: ["task-b"]
          - id: task-b
            depends_on: ["task-a"]
        ---
        """,
    )

    _run_cli(["--plan", str(plan_path)], expected_exit_code=1)

    captured = capsys.readouterr()
    assert "Error:" in captured.err


class TestThresholdFlag:
    _PLAN = """\
        ---
        subtasks:
          - id: task-a
            footprint: ["src/x.py", "src/1.py", "src/2.py", "src/3.py"]
          - id: task-b
            footprint: ["src/x.py", "src/9.py", "src/8.py", "src/7.py"]
        ---
        """

    def test_unspecified_threshold_preserves_default_behavior(self, tmp_path, capsys):
        plan_path = tmp_path / "plan.md"
        _write_plan(plan_path, self._PLAN)

        data = _run_cli_json(["--plan", str(plan_path)], capsys)

        assert data["edges"] == []

    def test_low_threshold_creates_similarity_edge(self, tmp_path, capsys):
        plan_path = tmp_path / "plan.md"
        _write_plan(plan_path, self._PLAN)

        data = _run_cli_json(["--plan", str(plan_path), "--threshold", "0.1"], capsys)

        assert {(e["source"], e["target"]) for e in data["edges"]} == {
            ("task-a", "task-b")
        }

    def test_high_threshold_removes_similarity_edge_present_by_default(
        self, tmp_path, capsys
    ):
        plan_path = tmp_path / "plan.md"
        _write_plan(
            plan_path,
            """\
            ---
            subtasks:
              - id: task-a
                footprint: ["src/x.py"]
                symbols: ["x.foo", "x.bar"]
              - id: task-b
                footprint: ["src/x.py"]
                symbols: ["x.foo", "x.baz"]
            ---
            """,
        )

        default_data = _run_cli_json(["--plan", str(plan_path)], capsys)
        assert len(default_data["edges"]) == 1

        high_threshold_data = _run_cli_json(
            ["--plan", str(plan_path), "--threshold", "0.99"], capsys
        )
        assert high_threshold_data["edges"] == []

    @pytest.mark.parametrize("bad_threshold", ["nan", "inf", "-inf", "2", "-0.5"])
    def test_threshold_outside_score_range_is_rejected(
        self, tmp_path, capsys, bad_threshold
    ):
        # 類似度スコアは[0, 1]の範囲に収まるため、NaN/Inf/範囲外の有限値は
        # 全エッジが黙って抑制されるだけの無意味な指定になる。明示的にエラーとして拒否する
        plan_path = tmp_path / "plan.md"
        _write_plan(plan_path, self._PLAN)

        _run_cli(
            ["--plan", str(plan_path), f"--threshold={bad_threshold}"],
            expected_exit_code=1,
        )

        captured = capsys.readouterr()
        assert "Error:" in captured.err
        assert "[0, 1]" in captured.err

    @pytest.mark.parametrize("boundary_threshold", ["0", "1"])
    def test_threshold_boundary_values_are_accepted(
        self, tmp_path, capsys, boundary_threshold
    ):
        plan_path = tmp_path / "plan.md"
        _write_plan(plan_path, self._PLAN)

        _run_cli(["--plan", str(plan_path), f"--threshold={boundary_threshold}"])


class TestIgnorePatternsConfig:
    _PLAN = """\
        ---
        subtasks:
          - id: task-a
            footprint: ["package.json", "src/a.py"]
          - id: task-b
            footprint: ["package.json", "src/b.py"]
        ---
        """

    def test_no_config_keeps_default_behavior(self, tmp_path, capsys):
        plan_path = tmp_path / "plan.md"
        _write_plan(plan_path, self._PLAN)

        data = _run_cli_json(["--plan", str(plan_path), "--threshold", "0.1"], capsys)

        assert {(e["source"], e["target"]) for e in data["edges"]} == {
            ("task-a", "task-b")
        }

    def test_orchestune_toml_ignore_pattern_excludes_manifest_collision(
        self, tmp_path, capsys
    ):
        plan_path = tmp_path / "plan.md"
        _write_plan(plan_path, self._PLAN)
        (tmp_path / "orchestune.toml").write_text(
            'dag_ignore_patterns = ["(^|/)package\\\\.json$"]\n',
            encoding="utf-8",
        )

        data = _run_cli_json(["--plan", str(plan_path), "--threshold", "0.1"], capsys)

        assert data["edges"] == []

    def test_pyproject_toml_tool_orchestune_ignore_pattern(self, tmp_path, capsys):
        plan_path = tmp_path / "plan.md"
        _write_plan(plan_path, self._PLAN)
        (tmp_path / "pyproject.toml").write_text(
            textwrap.dedent(
                """\
                [tool.orchestune]
                dag_ignore_patterns = ["(^|/)package\\\\.json$"]
                """
            ),
            encoding="utf-8",
        )

        data = _run_cli_json(["--plan", str(plan_path), "--threshold", "0.1"], capsys)

        assert data["edges"] == []

    def test_invalid_ignore_patterns_type_is_reported_as_error(self, tmp_path, capsys):
        plan_path = tmp_path / "plan.md"
        _write_plan(plan_path, self._PLAN)
        (tmp_path / "orchestune.toml").write_text(
            'dag_ignore_patterns = "not-a-list"\n',
            encoding="utf-8",
        )

        _run_cli(["--plan", str(plan_path)], expected_exit_code=1)

        captured = capsys.readouterr()
        assert "Error:" in captured.err
        assert "dag_ignore_patterns" in captured.err

    def test_invalid_regex_ignore_pattern_is_reported_as_error(self, tmp_path, capsys):
        plan_path = tmp_path / "plan.md"
        _write_plan(plan_path, self._PLAN)
        (tmp_path / "orchestune.toml").write_text(
            'dag_ignore_patterns = ["("]\n',
            encoding="utf-8",
        )

        _run_cli(["--plan", str(plan_path)], expected_exit_code=1)

        captured = capsys.readouterr()
        assert "Error:" in captured.err


class TestExtractDagIgnorePatterns:
    def test_missing_key_returns_empty_list(self):
        assert extract_dag_ignore_patterns({}) == []

    def test_underscore_key_is_read(self):
        config = {"dag_ignore_patterns": [r"(^|/)package\.json$"]}
        assert extract_dag_ignore_patterns(config) == [r"(^|/)package\.json$"]

    def test_hyphenated_key_is_accepted_as_alias(self):
        # #404レビュー指摘: この設定ファイルの他のキーは全てハイフン区切り
        # （max-concurrent等）のため、dag-ignore-patternsと書いても
        # サイレントに無視されず機能すること
        config = {"dag-ignore-patterns": [r"(^|/)package\.json$"]}
        assert extract_dag_ignore_patterns(config) == [r"(^|/)package\.json$"]

    def test_underscore_key_takes_precedence_over_hyphenated(self):
        config = {
            "dag_ignore_patterns": ["underscore"],
            "dag-ignore-patterns": ["hyphenated"],
        }
        assert extract_dag_ignore_patterns(config) == ["underscore"]

    def test_non_list_value_raises_value_error(self):
        with pytest.raises(ValueError, match="dag_ignore_patterns"):
            extract_dag_ignore_patterns({"dag_ignore_patterns": "not-a-list"})

    def test_non_string_element_raises_value_error(self):
        with pytest.raises(ValueError, match="dag_ignore_patterns"):
            extract_dag_ignore_patterns({"dag_ignore_patterns": [123]})


class TestLoadOrchestuneConfig:
    def test_missing_files_return_empty_dict(self, tmp_path):
        assert load_orchestune_config(tmp_path) == {}

    def test_reads_orchestune_toml_top_level(self, tmp_path):
        (tmp_path / "orchestune.toml").write_text(
            'dag_ignore_patterns = ["a.json"]\nmax-concurrent = 5\n',
            encoding="utf-8",
        )
        config = load_orchestune_config(tmp_path)
        assert config == {"dag_ignore_patterns": ["a.json"], "max-concurrent": 5}

    def test_falls_back_to_pyproject_toml_tool_orchestune_table(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(
            "[tool.orchestune]\n" 'dag_ignore_patterns = ["a.json"]\n',
            encoding="utf-8",
        )
        config = load_orchestune_config(tmp_path)
        assert config == {"dag_ignore_patterns": ["a.json"]}

    def test_orchestune_toml_takes_precedence_over_pyproject_toml(self, tmp_path):
        (tmp_path / "orchestune.toml").write_text(
            'dag_ignore_patterns = ["from-orchestune-toml"]\n', encoding="utf-8"
        )
        (tmp_path / "pyproject.toml").write_text(
            "[tool.orchestune]\n" 'dag_ignore_patterns = ["from-pyproject-toml"]\n',
            encoding="utf-8",
        )
        config = load_orchestune_config(tmp_path)
        assert config == {"dag_ignore_patterns": ["from-orchestune-toml"]}

    def test_invalid_orchestune_toml_raises_value_error(self, tmp_path):
        (tmp_path / "orchestune.toml").write_text(
            "not valid toml [[[", encoding="utf-8"
        )
        with pytest.raises(ValueError, match="orchestune.toml"):
            load_orchestune_config(tmp_path)

    def test_non_table_tool_orchestune_section_raises_value_error(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(
            "[tool]\norchestune = 1\n", encoding="utf-8"
        )
        with pytest.raises(ValueError, match="tool.orchestune"):
            load_orchestune_config(tmp_path)
