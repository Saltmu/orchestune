"""CLI/config-file behavior for `orchestune-dag` (issue #398)."""

import json
import sys
import textwrap

import pytest

from orchestune.dag_cli import main


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
