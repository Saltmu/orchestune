"""#393: DAG検証段階でfootprint/symbolsが実コードに実在するかを警告する。

`repo_root`を明示的に渡した場合にのみ実在確認を行うopt-in設計であることを
前提にしている: `build_dag`/`build_dag_from_plan`を`repo_root`無しで直接
呼ぶ既存の呼び出し元・テスト（架空の例示パスを使うものが多数ある）の挙動を
変えないため。実運用の入口である`orchestune-dag` CLIだけが`repo_root`に
cwdを渡して有効化する。
"""

import json
import sys
import textwrap

import pytest

from orchestune.dag_graph import build_dag
from orchestune.dag_models import SubTask


def _subtask(id_, footprint, symbols=(), depends_on=()):
    return SubTask(
        id=id_,
        description="",
        footprint=tuple(footprint),
        symbols=tuple(symbols),
        depends_on=tuple(depends_on),
        risk=False,
        risk_reasons=(),
    )


def _write_plan(tmp_path, content: str):
    path = tmp_path / "decomposition_plan.md"
    path.write_text(textwrap.dedent(content), encoding="utf-8")
    return path


class TestFootprintExistenceWarnings:
    def test_missing_footprint_path_produces_warning(self, tmp_path):
        subtasks = [_subtask("a", ["totally/made/up/path.py", "another/ghost.py"])]

        dag = build_dag(subtasks, repo_root=tmp_path)

        assert len(dag.warnings) == 1
        assert "a" in dag.warnings[0]
        assert "totally/made/up/path.py" in dag.warnings[0]
        assert "another/ghost.py" in dag.warnings[0]

    def test_existing_footprint_path_produces_no_warning(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "foo.py").write_text("x = 1\n", encoding="utf-8")
        subtasks = [_subtask("a", ["src/foo.py"])]

        dag = build_dag(subtasks, repo_root=tmp_path)

        assert dag.warnings == ()

    def test_repo_root_omitted_skips_existence_check(self):
        """`repo_root`を渡さない既存の呼び出し方では、実在確認自体が
        走らない（後方互換）。cwd（このリポジトリのルート）には存在しない
        架空のパスを使い、それでも警告が出ないことを確認する。"""
        subtasks = [_subtask("a", ["totally/made/up/path.py"])]

        dag = build_dag(subtasks)

        assert dag.warnings == ()

    def test_missing_symbols_are_surfaced_at_dag_stage(self, tmp_path):
        (tmp_path / "pkg").mkdir()
        (tmp_path / "pkg" / "mod.py").write_text(
            "def existing_function():\n    pass\n", encoding="utf-8"
        )
        subtasks = [
            _subtask("a", ["pkg/mod.py"], symbols=["NoSuchClass.no_such_method"])
        ]

        dag = build_dag(subtasks, repo_root=tmp_path)

        assert len(dag.warnings) == 1
        assert "a" in dag.warnings[0]
        assert "NoSuchClass.no_such_method" in dag.warnings[0]

    def test_new_file_only_footprint_does_not_block_dag_build(self, tmp_path):
        """新規作成予定のみのfootprintは正当な操作であり、警告は出ても
        DAG構築自体は例外を投げずに成功すること。"""
        subtasks = [
            _subtask("a", ["brand/new/file.py"]),
            _subtask("b", ["another/new/file.py"], depends_on=["a"]),
        ]

        dag = build_dag(subtasks, repo_root=tmp_path)

        assert dag.topological_order == ["a", "b"]
        assert len(dag.warnings) == 2


class TestCliSurfacesFootprintWarnings:
    def test_cli_text_output_includes_warnings_section(
        self, tmp_path, capsys, monkeypatch
    ):
        from orchestune.dag_cli import main

        plan_content = """\
        ---
        subtasks:
          - id: task-a
            footprint: ["totally/made/up/path.py"]
        ---
        """
        plan_path = _write_plan(tmp_path, plan_content)
        monkeypatch.chdir(tmp_path)

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
        assert "Warnings:" in captured.out
        assert "totally/made/up/path.py" in captured.out

    def test_cli_json_output_includes_warnings_array(
        self, tmp_path, capsys, monkeypatch
    ):
        from orchestune.dag_cli import main

        plan_content = """\
        ---
        subtasks:
          - id: task-a
            footprint: ["totally/made/up/path.py"]
        ---
        """
        plan_path = _write_plan(tmp_path, plan_content)
        monkeypatch.chdir(tmp_path)

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
        assert len(data["warnings"]) == 1
        assert "totally/made/up/path.py" in data["warnings"][0]

    def test_cli_exits_zero_for_new_file_only_footprint(
        self, tmp_path, capsys, monkeypatch
    ):
        """存在しないfootprintパスはエラーではなく警告であり、CLIをブロック
        しないこと（受け入れ基準）。"""
        from orchestune.dag_cli import main

        plan_content = """\
        ---
        subtasks:
          - id: task-a
            footprint: ["brand/new/file.py"]
        ---
        """
        plan_path = _write_plan(tmp_path, plan_content)
        monkeypatch.chdir(tmp_path)

        orig_argv = sys.argv
        sys.argv = ["orchestune-dag", "--plan", str(plan_path)]
        try:
            with pytest.raises(SystemExit) as excinfo:
                main()
            assert excinfo.value.code == 0
        finally:
            sys.argv = orig_argv
