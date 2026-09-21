"""Unit tests for AST and import-graph parsing internals in test_architecture."""

from __future__ import annotations

import ast

from test_architecture import (
    PACKAGE_ROOT,
    _collect_dict_assignments,
    _cycle_members,
    _internal_imports,
    _module_name,
    _relative_import_name,
)


def test_collect_dict_assignments_captures_annotated_assignments() -> None:
    """#531 review: git_cli.pyのkwargs: dict[str, Any] = {...} のような
    型注釈付き代入（ast.AnnAssign）も_collect_dict_assignmentsが正しく捕捉すること。"""
    code = """
kwargs: dict[str, Any] = {"text": True, "encoding": "utf-8"}
unannotated = {"text": True}
"""
    tree = ast.parse(code)
    assignments = _collect_dict_assignments(tree)
    assert "kwargs" in assignments
    assert "unannotated" in assignments


def test_module_name_resolves_subpackage_paths() -> None:
    """#614: _module_nameが直下・サブパッケージ・ネスト構造を正確にモジュール名へ解決すること。"""
    assert _module_name(PACKAGE_ROOT / "cli.py") == "orchestune.cli"
    assert _module_name(PACKAGE_ROOT / "__init__.py") == "orchestune"
    assert (
        _module_name(PACKAGE_ROOT / "forge" / "issues.py") == "orchestune.forge.issues"
    )
    assert _module_name(PACKAGE_ROOT / "forge" / "__init__.py") == "orchestune.forge"
    assert (
        _module_name(PACKAGE_ROOT / "dispatch" / "phase" / "gc.py")
        == "orchestune.dispatch.phase.gc"
    )
    assert (
        _module_name(PACKAGE_ROOT / "dispatch" / "phase" / "__init__.py")
        == "orchestune.dispatch.phase"
    )


def test_relative_import_resolution_for_subpackages() -> None:
    """#614: _relative_import_nameがサブパッケージ内外の相対インポートを正しく解決すること。"""
    # 1. サブモジュールから同一サブパッケージ内への相対インポート
    tree1 = ast.parse("from . import context")
    import_from1 = tree1.body[0]
    assert isinstance(import_from1, ast.ImportFrom)
    assert (
        _relative_import_name(
            "orchestune.dispatch.cycle", import_from1, is_package=False
        )
        == "orchestune.dispatch"
    )

    tree2 = ast.parse("from .context import Context")
    import_from2 = tree2.body[0]
    assert isinstance(import_from2, ast.ImportFrom)
    assert (
        _relative_import_name(
            "orchestune.dispatch.cycle", import_from2, is_package=False
        )
        == "orchestune.dispatch.context"
    )

    # 2. サブモジュールから親パッケージ／別サブパッケージへの相対インポート
    tree3 = ast.parse("from ..forge import issues")
    import_from3 = tree3.body[0]
    assert isinstance(import_from3, ast.ImportFrom)
    assert (
        _relative_import_name(
            "orchestune.dispatch.cycle", import_from3, is_package=False
        )
        == "orchestune.forge"
    )

    tree4 = ast.parse("from .. import git_cli")
    import_from4 = tree4.body[0]
    assert isinstance(import_from4, ast.ImportFrom)
    assert (
        _relative_import_name(
            "orchestune.dispatch.cycle", import_from4, is_package=False
        )
        == "orchestune"
    )

    # 3. サブパッケージの __init__.py からの相対インポート
    tree5 = ast.parse("from . import cycle")
    import_from5 = tree5.body[0]
    assert isinstance(import_from5, ast.ImportFrom)
    assert (
        _relative_import_name("orchestune.dispatch", import_from5, is_package=True)
        == "orchestune.dispatch"
    )

    tree6 = ast.parse("from .cycle import run")
    import_from6 = tree6.body[0]
    assert isinstance(import_from6, ast.ImportFrom)
    assert (
        _relative_import_name("orchestune.dispatch", import_from6, is_package=True)
        == "orchestune.dispatch.cycle"
    )

    # 4. パッケージ境界を超える相対インポート
    tree7 = ast.parse("from ... import outside")
    import_from7 = tree7.body[0]
    assert isinstance(import_from7, ast.ImportFrom)
    assert (
        _relative_import_name("orchestune.cli", import_from7, is_package=False) is None
    )

    # 深い階層からパッケージ境界を大幅に超えるケース（負のスライスインデックスによる誤解決の防止）
    tree8 = ast.parse("from ..... import way_outside")
    import_from8 = tree8.body[0]
    assert isinstance(import_from8, ast.ImportFrom)
    assert (
        _relative_import_name(
            "orchestune.dispatch.phase.gc", import_from8, is_package=False
        )
        is None
    )


def test_internal_imports_capture_subpackage_dependencies() -> None:
    """#614: _internal_importsがサブパッケージ間の依存関係を正しく網羅すること。"""
    known_modules = {
        "orchestune",
        "orchestune.dispatch",
        "orchestune.dispatch.cycle",
        "orchestune.dispatch.context",
        "orchestune.forge",
        "orchestune.forge.issues",
        "orchestune.git_cli",
    }
    source = """
from . import context
from .context import Context
from ..forge import issues
from ..forge.issues import IssueRecord
from ..git_cli import run_git
import orchestune.forge
import os
"""
    tree = ast.parse(source)
    imports = _internal_imports(
        "orchestune.dispatch.cycle", tree, known_modules, is_package=False
    )
    assert imports == {
        "orchestune.dispatch",
        "orchestune.dispatch.context",
        "orchestune.forge",
        "orchestune.forge.issues",
        "orchestune.git_cli",
    }


def test_import_graph_detects_subpackage_cycles() -> None:
    """#614: _cycle_membersがサブパッケージをまたぐ循環依存を検出できること。"""
    graph_with_cycle = {
        "dispatch.cycle": {"forge.issues"},
        "forge.issues": {"dispatch.cycle"},
        "dag_graph": {"models"},
    }
    assert _cycle_members(graph_with_cycle) == {"dispatch.cycle", "forge.issues"}

    graph_without_cycle = {
        "dispatch.cycle": {"forge.issues"},
        "forge.issues": {"models"},
        "models": set(),
    }
    assert _cycle_members(graph_without_cycle) == set()


def test_relative_import_resolution_multilevel() -> None:
    """#614: 深い階層のサブパッケージからの多階層相対インポート解決。"""
    tree = ast.parse("from ...forge.issues import IssueRecord")
    import_from = tree.body[0]
    assert isinstance(import_from, ast.ImportFrom)
    assert (
        _relative_import_name(
            "orchestune.dispatch.phase.gc", import_from, is_package=False
        )
        == "orchestune.forge.issues"
    )
