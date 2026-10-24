"""footprint の値（リポジトリ相対の POSIX パスの列）の正規化・検証のテスト。

`orchestune.footprint_paths` は L0 の共有部品で、claim・ledger・Issue 解析・DAG が
使う。もとは `dag.models` にあり（#1320）、3クラスは test_dag_graph.py から
中身を変えずに移した。
"""

import ast
import sys
from pathlib import Path

import pytest
from architecture_source_support import _source_ast

from orchestune import footprint_paths
from orchestune.footprint_paths import (
    FootprintValidationError,
    canonicalize_footprint,
    normalize_footprint_path,
    parse_footprint_value,
)

_MOVED_SYMBOLS = frozenset(
    {
        "normalize_footprint_path",
        "parse_footprint_value",
        "canonicalize_footprint",
        "FootprintValidationError",
    }
)
_REPO_ROOT = Path(__file__).parents[1]


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


class TestParseFootprintValue:
    def test_parses_none_and_empty_list_as_empty_tuple(self):
        assert parse_footprint_value(None) == ()
        assert parse_footprint_value([]) == ()
        assert parse_footprint_value(()) == ()

    def test_rejects_non_list_types(self):
        for invalid in ("src/core.py", {"path": "src/core.py"}, 42, True):
            with pytest.raises(FootprintValidationError, match="must be a list"):
                parse_footprint_value(invalid)

    def test_normalizes_and_deduplicates_paths(self):
        raw = ["./src/core.py", "src\\utils.py", "src/core.py", "./src//utils.py"]
        assert parse_footprint_value(raw) == ("src/core.py", "src/utils.py")

    def test_skips_none_elements_and_stringifies_numbers(self):
        raw = ["src/a.py", None, 123, 45.6]
        assert parse_footprint_value(raw) == ("src/a.py", "123", "45.6")

    def test_rejects_bool_and_complex_elements(self):
        for invalid in [True, False, {"a": 1}, ["nested"]]:
            with pytest.raises(
                FootprintValidationError, match="Invalid footprint element"
            ):
                parse_footprint_value(["src/a.py", invalid])

    def test_rejects_invalid_paths_with_validation_error(self):
        for bad_path in ("/abs/path.py", "C:\\win\\path.py", "../escape.py", "", "."):
            with pytest.raises(FootprintValidationError):
                parse_footprint_value(["src/ok.py", bad_path])


class TestCanonicalizeFootprint:
    def test_normalizes_valid_paths(self):
        paths = ["./src/a.py", "src\\b.py", "src/a.py"]
        assert canonicalize_footprint(paths) == ("src/a.py", "src/b.py")

    def test_retains_invalid_paths_as_raw_strings(self):
        paths = ["/abs/path.py", "../escape.py", "./src/a.py", ""]
        assert canonicalize_footprint(paths) == (
            "/abs/path.py",
            "../escape.py",
            "src/a.py",
            "",
        )


def _python_sources() -> list[Path]:
    return sorted(
        path
        for root in ("orchestune", "tests")
        for path in (_REPO_ROOT / root).rglob("*.py")
        if path != Path(__file__)
    )


def test_footprint_paths_imports_only_the_standard_library() -> None:
    tree = _source_ast(Path(footprint_paths.__file__))
    imported: set[str] = set()
    for node in ast.walk(tree):
        assert not (
            isinstance(node, ast.ImportFrom) and node.level
        ), "relative import would hide a package dependency"
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    imported.discard("__future__")
    assert imported, "no imports found: the scan is broken"
    assert imported <= set(sys.stdlib_module_names), imported


def test_dag_models_no_longer_exposes_the_moved_symbols() -> None:
    from orchestune.dag import models

    assert sorted(name for name in _MOVED_SYMBOLS if hasattr(models, name)) == []


def test_no_source_takes_the_moved_symbols_from_dag_models() -> None:
    """Imports are checked on the AST, so multi-line, aliased and relative forms count."""
    offenders = []
    for path in _python_sources():
        tree = _source_ast(path)
        is_dag_package = path.parent == _REPO_ROOT / "orchestune" / "dag"
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                from_models = node.module == "orchestune.dag.models" or (
                    is_dag_package and node.level == 1 and node.module == "models"
                )
                if from_models:
                    offenders += [
                        f"{path.name}:{node.lineno} imports {alias.name}"
                        for alias in node.names
                        if alias.name in _MOVED_SYMBOLS
                    ]
            elif isinstance(node, ast.Attribute) and node.attr in _MOVED_SYMBOLS:
                # `models.normalize_footprint_path(...)` / `dag.models.canonicalize...`
                if ast.unparse(node.value).split(".")[-1] == "models":
                    offenders.append(f"{path.name}:{node.lineno} uses .{node.attr}")
    assert offenders == []
