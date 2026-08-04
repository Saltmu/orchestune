"""#359: Footprint.symbolsが実コードに実在するかの検証（symbol_verification.py）。"""

from pathlib import Path

from orchestune.dag_models import SubTask
from orchestune.symbol_verification import find_missing_symbols


def _subtask(footprint: tuple[str, ...] = (), symbols: tuple[str, ...] = ()) -> SubTask:
    return SubTask(
        id="task-a",
        description="",
        footprint=footprint,
        symbols=symbols,
        depends_on=(),
        risk=False,
        risk_reasons=(),
    )


def _write(repo_root: Path, relative_path: str, content: str) -> None:
    path = repo_root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


class TestFindMissingSymbols:
    def test_bare_function_name_found(self, tmp_path):
        _write(tmp_path, "pkg/mod.py", "def format_dispatch_report():\n    pass\n")
        subtask = _subtask(
            footprint=("pkg/mod.py",), symbols=("format_dispatch_report",)
        )

        assert find_missing_symbols(subtask, tmp_path) == ()

    def test_bare_class_name_found(self, tmp_path):
        _write(tmp_path, "pkg/mod.py", "class Foo:\n    pass\n")
        subtask = _subtask(footprint=("pkg/mod.py",), symbols=("Foo",))

        assert find_missing_symbols(subtask, tmp_path) == ()

    def test_class_method_qualified_name_found(self, tmp_path):
        _write(
            tmp_path,
            "pkg/mod.py",
            "class IntegrationMerger:\n"
            "    def merge_and_test_tasks(self):\n"
            "        pass\n",
        )
        subtask = _subtask(
            footprint=("pkg/mod.py",),
            symbols=("IntegrationMerger.merge_and_test_tasks",),
        )

        assert find_missing_symbols(subtask, tmp_path) == ()

    def test_module_qualified_name_matches_via_leaf_segment(self, tmp_path):
        """docs/en/usage.md・skills/orchestune/SKILL.mdは`db.get_connection`
        のような「(モジュール/サブシステム名).symbol」記法を例示している。
        この接頭辞はASTでは追跡しないPython外の自由記述ラベルのため、
        末尾セグメントでの一致も許容する（レビュー指摘 #372）。"""
        _write(tmp_path, "src/db/connection.py", "def get_connection():\n    pass\n")
        subtask = _subtask(
            footprint=("src/db/connection.py",), symbols=("db.get_connection",)
        )

        assert find_missing_symbols(subtask, tmp_path) == ()

    def test_bare_method_name_found_without_class_qualification(self, tmp_path):
        _write(
            tmp_path,
            "pkg/mod.py",
            "class IntegrationMerger:\n"
            "    def merge_and_test_tasks(self):\n"
            "        pass\n",
        )
        subtask = _subtask(footprint=("pkg/mod.py",), symbols=("merge_and_test_tasks",))

        assert find_missing_symbols(subtask, tmp_path) == ()

    def test_top_level_constant_found(self, tmp_path):
        _write(tmp_path, "pkg/mod.py", "DEFAULT_TIMEOUT = 30\n")
        subtask = _subtask(footprint=("pkg/mod.py",), symbols=("DEFAULT_TIMEOUT",))

        assert find_missing_symbols(subtask, tmp_path) == ()

    def test_missing_symbol_is_reported(self, tmp_path):
        _write(tmp_path, "pkg/mod.py", "def write_github_step_summary():\n    pass\n")
        subtask = _subtask(
            footprint=("pkg/mod.py",), symbols=("format_dispatch_report",)
        )

        assert find_missing_symbols(subtask, tmp_path) == ("format_dispatch_report",)

    def test_symbols_aggregated_across_multiple_footprint_files(self, tmp_path):
        _write(tmp_path, "pkg/a.py", "def foo():\n    pass\n")
        _write(tmp_path, "pkg/b.py", "def bar():\n    pass\n")
        subtask = _subtask(
            footprint=("pkg/a.py", "pkg/b.py"), symbols=("foo", "bar", "missing")
        )

        assert find_missing_symbols(subtask, tmp_path) == ("missing",)

    def test_nonexistent_footprint_file_is_skipped_without_crashing(self, tmp_path):
        _write(tmp_path, "pkg/a.py", "def foo():\n    pass\n")
        subtask = _subtask(
            footprint=("pkg/a.py", "pkg/does_not_exist.py"), symbols=("foo",)
        )

        assert find_missing_symbols(subtask, tmp_path) == ()

    def test_all_footprint_files_missing_returns_empty_to_avoid_false_positive(
        self, tmp_path
    ):
        subtask = _subtask(
            footprint=("pkg/not_yet_created.py",), symbols=("brand_new_function",)
        )

        assert find_missing_symbols(subtask, tmp_path) == ()

    def test_no_footprint_returns_empty(self, tmp_path):
        subtask = _subtask(footprint=(), symbols=("anything",))

        assert find_missing_symbols(subtask, tmp_path) == ()

    def test_non_python_footprint_files_are_ignored(self, tmp_path):
        _write(tmp_path, "docs/readme.md", "# format_dispatch_report\n")
        subtask = _subtask(
            footprint=("docs/readme.md",), symbols=("format_dispatch_report",)
        )

        # マークダウン中に文字列として出現していても、Pythonの識別子として
        # 定義されているわけではないため検証材料にならず、falls back to empty.
        assert find_missing_symbols(subtask, tmp_path) == ()

    def test_no_symbols_returns_empty(self, tmp_path):
        _write(tmp_path, "pkg/mod.py", "def foo():\n    pass\n")
        subtask = _subtask(footprint=("pkg/mod.py",), symbols=())

        assert find_missing_symbols(subtask, tmp_path) == ()

    def test_nested_function_definition_is_found(self, tmp_path):
        _write(
            tmp_path,
            "pkg/mod.py",
            "def outer():\n    def inner_helper():\n        pass\n    return inner_helper\n",
        )
        subtask = _subtask(footprint=("pkg/mod.py",), symbols=("inner_helper",))

        assert find_missing_symbols(subtask, tmp_path) == ()

    def test_unparseable_python_file_is_skipped_without_crashing(self, tmp_path):
        _write(tmp_path, "pkg/broken.py", "def this is not valid python(\n")
        subtask = _subtask(footprint=("pkg/broken.py",), symbols=("anything",))

        assert find_missing_symbols(subtask, tmp_path) == ()
