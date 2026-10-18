"""Tests for architecture source and AST caching support (Issue #1366)."""

from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import patch

import pytest
from architecture_source_support import (
    _clear_source_caches,
    _source_ast,
    _source_text,
)
from architecture_test_support import (
    _clear_package_caches,
    _package_asts,
    _package_modules,
    _package_sources,
)


def test_source_text_caches_and_normalizes_paths(tmp_path: Path) -> None:
    _clear_source_caches()
    try:
        sample_file = tmp_path / "sample.py"
        sample_file.write_text("x = 1\n", encoding="utf-8")

        # Call with relative-like / resolved paths
        resolved = sample_file.resolve()
        assert _source_text(sample_file) == "x = 1\n"
        assert _source_text(resolved) == "x = 1\n"

        # Verify disk is only read once across identical resolved paths
        with patch.object(
            Path, "read_text", side_effect=AssertionError("Disk read occurred!")
        ):
            assert _source_text(sample_file) == "x = 1\n"
            assert _source_text(resolved) == "x = 1\n"
    finally:
        _clear_source_caches()


def test_source_ast_caches_and_sets_filename(tmp_path: Path) -> None:
    _clear_source_caches()
    try:
        sample_file = tmp_path / "sample_ast.py"
        sample_file.write_text("y = 42\n", encoding="utf-8")

        tree1 = _source_ast(sample_file)
        assert isinstance(tree1, ast.Module)
        assert isinstance(tree1.body[0], ast.Assign)

        # Caching: returns the same AST object
        tree2 = _source_ast(sample_file)
        assert tree1 is tree2

        # Verify ast.parse is not called again
        with patch("ast.parse", side_effect=AssertionError("ast.parse called again!")):
            tree3 = _source_ast(sample_file)
            assert tree3 is tree1
    finally:
        _clear_source_caches()


def test_source_errors_propagate(tmp_path: Path) -> None:
    _clear_source_caches()
    try:
        non_existent = tmp_path / "does_not_exist.py"
        with pytest.raises(OSError):
            _source_text(non_existent)

        syntax_error_file = tmp_path / "syntax_error.py"
        syntax_error_file.write_text("def broken(:\n", encoding="utf-8")
        with pytest.raises(SyntaxError):
            _source_ast(syntax_error_file)

        non_utf8_file = tmp_path / "binary.py"
        non_utf8_file.write_bytes(b"\x80abc")
        with pytest.raises(UnicodeDecodeError):
            _source_text(non_utf8_file)
    finally:
        _clear_source_caches()


def test_clear_source_caches_invalidates_read_and_ast(tmp_path: Path) -> None:
    _clear_source_caches()
    try:
        sample_file = tmp_path / "mutated.py"
        sample_file.write_text("v = 1\n", encoding="utf-8")
        assert _source_text(sample_file) == "v = 1\n"
        tree1 = _source_ast(sample_file)
        assert isinstance(tree1.body[0].value, ast.Constant)
        assert tree1.body[0].value.value == 1

        # Mutate file on disk
        sample_file.write_text("v = 2\n", encoding="utf-8")

        # Before clear, cached content is returned
        assert _source_text(sample_file) == "v = 1\n"
        assert _source_ast(sample_file) is tree1

        # After clear, fresh content and AST are loaded
        _clear_source_caches()
        assert _source_text(sample_file) == "v = 2\n"
        tree2 = _source_ast(sample_file)
        assert tree2 is not tree1
        assert tree2.body[0].value.value == 2
    finally:
        _clear_source_caches()


def test_package_sources_and_asts_views() -> None:
    _clear_source_caches()
    _clear_package_caches()
    try:
        modules = _package_modules()
        sources = _package_sources()
        asts = _package_asts()

        assert set(sources.keys()) == set(modules.keys())
        assert set(asts.keys()) == set(modules.keys())

        # Ensure "orchestune" and subpackage modules are present
        assert "orchestune" in sources
        assert "orchestune.cli" in sources
        assert "orchestune" in asts
        assert "orchestune.cli" in asts

        # Read-only mapping check: mutating raises TypeError
        with pytest.raises(TypeError):
            sources["orchestune"] = "tampered"  # type: ignore[index]
        with pytest.raises(TypeError):
            asts["orchestune"] = ast.Module(body=[], type_ignores=[])  # type: ignore[index]

        # Verify asts contain valid ast.Module instances matching sources
        tree = asts["orchestune.cli"]
        assert isinstance(tree, ast.Module)
    finally:
        _clear_source_caches()
        _clear_package_caches()


def test_boundary_observations_and_ast_consistency() -> None:
    from active_worktree_architecture_support import (
        _active_worktree_boundary_violations_ast,
        _clear_active_worktree_observations,
        active_worktree_boundary_violations,
    )
    from active_worktree_architecture_support import (
        _production_observations as _active_observations,
    )
    from dependency_boundary_test_support import (
        _boundary_violations_ast,
        _clear_dependency_boundary_observations,
        boundary_violations,
    )
    from dependency_boundary_test_support import (
        _production_observations as _dep_observations,
    )
    from test_architecture import REPO_ROOT

    source = """
from orchestune.dispatch.active_worktrees import ActiveWorktree
def check(w: ActiveWorktree):
    return w.pid
"""
    # 1. Source vs AST input consistency on active_worktree
    tree = ast.parse(source)
    v_src = active_worktree_boundary_violations(source, module="orchestune.test_mod")
    v_ast = _active_worktree_boundary_violations_ast(tree, module="orchestune.test_mod")
    assert v_src == v_ast

    # 2. Source vs AST input consistency on dependency boundary
    dep_src = """
def check(ctx):
    return ctx.branch_by_issue_number
"""
    dep_tree = ast.parse(dep_src)
    d_src = boundary_violations(dep_src, module="orchestune.dispatch.policy")
    d_ast = _boundary_violations_ast(dep_tree, module="orchestune.dispatch.policy")
    assert d_src == d_ast

    # 3. Observation caching per normalized repo root
    _clear_active_worktree_observations()
    _clear_dependency_boundary_observations()
    try:
        obs1 = _active_observations(REPO_ROOT)
        obs2 = _active_observations(REPO_ROOT.resolve())
        assert obs1 is obs2

        dep1 = _dep_observations(REPO_ROOT)
        dep2 = _dep_observations(REPO_ROOT.resolve())
        assert dep1 is dep2
    finally:
        _clear_active_worktree_observations()
        _clear_dependency_boundary_observations()


def test_forge_patch_ast_cache_and_consistency() -> None:
    from test_architecture_forge_patch import (
        _clear_forge_patch_caches,
        _github_forge_patch_lines,
        _github_forge_patch_lines_ast,
        _test_module_asts,
    )

    source = """
from unittest.mock import patch
def test_foo():
    with patch("orchestune.forge.GitHubForge.list_prs"):
        pass
"""
    tree = ast.parse(source)
    lines_src = _github_forge_patch_lines(source)
    lines_ast = _github_forge_patch_lines_ast(tree)
    assert lines_src == lines_ast == [4]

    _clear_forge_patch_caches()
    try:
        mods1 = _test_module_asts()
        mods2 = _test_module_asts()
        assert mods1 is mods2
        assert len(mods1) > 0
        assert all(isinstance(t, ast.Module) for _p, _s, t in mods1)
    finally:
        _clear_forge_patch_caches()


def test_pyproject_and_architecture_doc_immutability() -> None:
    from test_architecture import (
        _architecture_doc,
        _clear_architecture_test_caches,
        _pyproject,
    )

    _clear_architecture_test_caches()
    try:
        p1 = _pyproject()
        p1["mutated"] = True
        p1["tool"]["hatch"]["build"]["targets"]["tampered"] = 123

        p2 = _pyproject()
        assert "mutated" not in p2
        assert "tampered" not in p2["tool"]["hatch"]["build"]["targets"]

        # architecture doc returns a fresh list
        d1 = _architecture_doc("ja")
        d1.append("TAMPERED LINE")
        d2 = _architecture_doc("ja")
        assert "TAMPERED LINE" not in d2
    finally:
        _clear_architecture_test_caches()


def test_ast_dump_unmutated_across_inspections() -> None:
    from architecture_test_support import _package_asts

    asts = _package_asts()
    cli_ast = asts["orchestune.cli"]
    dump_before = ast.dump(cli_ast, include_attributes=True)

    # Walk or inspect ast
    _ = [node for node in ast.walk(cli_ast)]

    dump_after = ast.dump(cli_ast, include_attributes=True)
    assert dump_before == dump_after
