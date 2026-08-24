"""CLI/config-file behavior for `orchestune-dag` (issue #398)."""

import json
import sys
import textwrap

import pytest

from orchestune.dag.cli import main
from orchestune.dag.models import (
    extract_dag_ignore_patterns,
    extract_dag_similarity_threshold,
    load_orchestune_config,
)


def _write_plan(path, content: str) -> None:
    path.write_text(textwrap.dedent(content), encoding="utf-8")


def _mark_git_repository(path) -> None:
    git_dir = path / ".git"
    git_dir.mkdir()
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")


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


class TestRepoRootDiscovery:
    """#410: サブディレクトリのplanでもGitリポジトリルートを基準にする。"""

    def test_nested_plan_validates_footprint_from_repository_root(
        self, tmp_path, capsys
    ):
        _mark_git_repository(tmp_path)
        (tmp_path / "plans").mkdir()
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "existing.py").write_text("", encoding="utf-8")
        plan_path = tmp_path / "plans" / "decomposition_plan.md"
        _write_plan(
            plan_path,
            """\
            ---
            subtasks:
              - id: task-a
                footprint: ["src/existing.py"]
            ---
            """,
        )

        data = _run_cli_json(["--plan", str(plan_path)], capsys)

        assert data["warnings"] == []

    def test_nested_plan_loads_ignore_patterns_from_repository_root(
        self, tmp_path, capsys
    ):
        _mark_git_repository(tmp_path)
        (tmp_path / "plans").mkdir()
        (tmp_path / "orchestune.toml").write_text(
            'dag_ignore_patterns = ["(^|/)package\\\\.json$"]\n',
            encoding="utf-8",
        )
        plan_path = tmp_path / "plans" / "decomposition_plan.md"
        _write_plan(plan_path, TestIgnorePatternsConfig._PLAN)

        data = _run_cli_json(["--plan", str(plan_path), "--threshold", "0.1"], capsys)

        assert data["edges"] == []


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
            expected_exit_code=2,
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

        _run_cli(["--plan", str(plan_path)], expected_exit_code=2)

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

        _run_cli(["--plan", str(plan_path)], expected_exit_code=2)

        captured = capsys.readouterr()
        assert "Error:" in captured.err

    def test_empty_string_ignore_pattern_is_reported_as_error(self, tmp_path, capsys):
        plan_path = tmp_path / "plan.md"
        _write_plan(plan_path, self._PLAN)
        (tmp_path / "orchestune.toml").write_text(
            'dag_ignore_patterns = [""]\n',
            encoding="utf-8",
        )

        _run_cli(["--plan", str(plan_path)], expected_exit_code=2)

        captured = capsys.readouterr()
        assert "Error:" in captured.err
        assert "non-empty" in captured.err


class TestSimilarityThresholdConfig:
    """#407: `--threshold`未指定時、`dag_similarity_threshold`設定ファイルの
    値が`DEFAULT_SIMILARITY_THRESHOLD`より優先されること。CLIフラグは
    設定ファイルより常に優先される。"""

    _PLAN = TestThresholdFlag._PLAN

    def test_no_config_keeps_default_behavior(self, tmp_path, capsys):
        plan_path = tmp_path / "plan.md"
        _write_plan(plan_path, self._PLAN)

        data = _run_cli_json(["--plan", str(plan_path)], capsys)

        assert data["edges"] == []

    def test_config_threshold_lowers_effective_threshold(self, tmp_path, capsys):
        plan_path = tmp_path / "plan.md"
        _write_plan(plan_path, self._PLAN)
        (tmp_path / "orchestune.toml").write_text(
            "dag_similarity_threshold = 0.1\n", encoding="utf-8"
        )

        data = _run_cli_json(["--plan", str(plan_path)], capsys)

        assert {(e["source"], e["target"]) for e in data["edges"]} == {
            ("task-a", "task-b")
        }

    def test_pyproject_toml_tool_orchestune_threshold(self, tmp_path, capsys):
        plan_path = tmp_path / "plan.md"
        _write_plan(plan_path, self._PLAN)
        (tmp_path / "pyproject.toml").write_text(
            textwrap.dedent(
                """\
                [tool.orchestune]
                dag_similarity_threshold = 0.1
                """
            ),
            encoding="utf-8",
        )

        data = _run_cli_json(["--plan", str(plan_path)], capsys)

        assert {(e["source"], e["target"]) for e in data["edges"]} == {
            ("task-a", "task-b")
        }

    def test_cli_flag_takes_precedence_over_config(self, tmp_path, capsys):
        plan_path = tmp_path / "plan.md"
        _write_plan(plan_path, self._PLAN)
        (tmp_path / "orchestune.toml").write_text(
            "dag_similarity_threshold = 0.1\n", encoding="utf-8"
        )

        data = _run_cli_json(["--plan", str(plan_path), "--threshold", "0.99"], capsys)

        assert data["edges"] == []

    def test_invalid_threshold_type_is_reported_as_error(self, tmp_path, capsys):
        plan_path = tmp_path / "plan.md"
        _write_plan(plan_path, self._PLAN)
        (tmp_path / "orchestune.toml").write_text(
            'dag_similarity_threshold = "not-a-number"\n', encoding="utf-8"
        )

        _run_cli(["--plan", str(plan_path)], expected_exit_code=2)

        captured = capsys.readouterr()
        assert "Error:" in captured.err
        assert "dag_similarity_threshold" in captured.err

    def test_out_of_range_threshold_is_reported_as_error(self, tmp_path, capsys):
        plan_path = tmp_path / "plan.md"
        _write_plan(plan_path, self._PLAN)
        (tmp_path / "orchestune.toml").write_text(
            "dag_similarity_threshold = 2\n", encoding="utf-8"
        )

        _run_cli(["--plan", str(plan_path)], expected_exit_code=2)

        captured = capsys.readouterr()
        assert "Error:" in captured.err
        assert "[0, 1]" in captured.err


class TestExtractDagSimilarityThreshold:
    def test_missing_key_returns_none(self):
        assert extract_dag_similarity_threshold({}) is None

    def test_underscore_key_is_read(self):
        assert (
            extract_dag_similarity_threshold({"dag_similarity_threshold": 0.3}) == 0.3
        )

    def test_hyphenated_key_is_accepted_as_alias(self):
        config = {"dag-similarity-threshold": 0.3}
        assert extract_dag_similarity_threshold(config) == 0.3

    def test_underscore_key_takes_precedence_over_hyphenated(self):
        config = {
            "dag_similarity_threshold": 0.1,
            "dag-similarity-threshold": 0.9,
        }
        assert extract_dag_similarity_threshold(config) == 0.1

    def test_integer_value_is_coerced_to_float(self):
        assert extract_dag_similarity_threshold({"dag_similarity_threshold": 1}) == 1.0

    def test_boolean_value_raises_value_error(self):
        # #404のdag_ignore_patternsレビュー指摘と同様: bool は int のサブクラスの
        # ため、isinstance(value, int)だけだとTrue/Falseが1.0/0.0としてすり抜ける
        with pytest.raises(ValueError, match="dag_similarity_threshold"):
            extract_dag_similarity_threshold({"dag_similarity_threshold": True})

    def test_non_numeric_value_raises_value_error(self):
        with pytest.raises(ValueError, match="dag_similarity_threshold"):
            extract_dag_similarity_threshold({"dag_similarity_threshold": "0.5"})

    def test_out_of_range_value_raises_value_error(self):
        with pytest.raises(ValueError, match=r"\[0, 1\]"):
            extract_dag_similarity_threshold({"dag_similarity_threshold": 1.5})

    @pytest.mark.parametrize("boundary", [0, 1, 0.0, 1.0])
    def test_boundary_values_are_accepted(self, boundary):
        assert extract_dag_similarity_threshold(
            {"dag_similarity_threshold": boundary}
        ) == float(boundary)


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

    def test_empty_string_element_raises_value_error(self):
        # #404レビュー指摘: 空文字列はre.compile("")で有効な正規表現になり
        # あらゆるパスにマッチしてしまう（=全ての類似度エッジが無診断で
        # 消える）ため、明示的に拒否する
        with pytest.raises(ValueError, match="non-empty"):
            extract_dag_ignore_patterns({"dag_ignore_patterns": [""]})


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

    def test_non_table_tool_section_raises_config_error_not_attribute_error(
        self, tmp_path
    ):
        """Codex review (#441): a syntactically valid `pyproject.toml` whose
        top-level `tool` key isn't a table (e.g. `tool = "not-a-table"`) must
        not reach `.get("orchestune", ...)` on a non-dict and blow up with an
        uncaught `AttributeError` — it should be reported as the same
        `ConfigError` as any other malformed config, so `orchestune-dag`/
        `orchestune-provision`/`orchestune-dispatch` all map it to exit 2
        rather than an uncaught traceback."""
        (tmp_path / "pyproject.toml").write_text(
            'tool = "not-a-table"\n', encoding="utf-8"
        )
        with pytest.raises(ValueError, match=r"\[tool\]"):
            load_orchestune_config(tmp_path)
