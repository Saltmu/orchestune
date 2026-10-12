"""Unit tests for the cross-package private import detector (#1321)."""

from __future__ import annotations

import ast

from architecture_test_support import _cross_package_private_imports_in_tree


def _scan(
    module: str, source: str, *, is_package: bool = False
) -> set[tuple[str, str, str]]:
    found: set[tuple[str, str, str]] = _cross_package_private_imports_in_tree(
        module, ast.parse(source), is_package=is_package
    )
    return found


def test_reports_private_name_imported_from_another_package() -> None:
    found = _scan(
        "orchestune.recovery.service", "from orchestune.dispatch.x import _hidden"
    )
    assert found == {("recovery.service", "dispatch.x", "_hidden")}


def test_reports_original_name_not_alias() -> None:
    found = _scan("orchestune.a.m", "from orchestune.b.n import _hidden as visible")
    assert found == {("a.m", "b.n", "_hidden")}


def test_ignores_public_names_and_dunders() -> None:
    source = "from orchestune.b.n import public, __version__"
    assert _scan("orchestune.a.m", source) == set()


def test_ignores_same_top_level_package() -> None:
    assert _scan("orchestune.a.m", "from orchestune.a.n import _hidden") == set()


def test_ignores_external_libraries() -> None:
    assert _scan("orchestune.a.m", "from os.path import _joinrealpath") == set()


def test_handles_relative_imports_and_package_initialisers() -> None:
    assert _scan("orchestune.a.m", "from ..b.n import _hidden") == {
        ("a.m", "b.n", "_hidden")
    }
    assert _scan("orchestune.a", "from ..b import _hidden", is_package=True) == {
        ("a", "b", "_hidden")
    }


def test_handles_multiline_imports() -> None:
    source = "from orchestune.b.n import (\n    ok,\n    _one,\n    _two,\n)\n"
    assert _scan("orchestune.a.m", source) == {
        ("a.m", "b.n", "_one"),
        ("a.m", "b.n", "_two"),
    }
