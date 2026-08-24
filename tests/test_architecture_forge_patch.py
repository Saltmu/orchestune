"""Architectural invariants verifying GitHubForge and removed github compatibility modules are not patched."""

from __future__ import annotations

import ast
import re
from collections import defaultdict
from pathlib import Path

PACKAGE_ROOT = Path(__file__).parents[1] / "orchestune"
TESTS_ROOT = Path(__file__).parent
REPO_ROOT = Path(__file__).parents[1]

GITHUB_FORGE_PATCH_EXEMPTIONS = frozenset({"test_forge.py"})
_GITHUB_FORGE_PATCH_TARGET = re.compile(r"(?:^|\.)GitHubForge(?:\.|$)")
_SCOPE_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)


def _assigned_names(node: ast.AST) -> tuple[list[ast.expr], ast.expr | None]:
    if isinstance(node, ast.Assign):
        return list(node.targets), node.value
    if isinstance(node, ast.AnnAssign | ast.AugAssign):
        return [node.target], node.value
    return [], None


def _nodes_in_scope(scope: ast.AST) -> list[ast.AST]:
    """`scope` 直下のノードを、ネストしたスコープの中身を除いて位置順に返す。"""
    collected: list[ast.AST] = []

    def visit(node: ast.AST, *, is_root: bool) -> None:
        if not is_root:
            collected.append(node)
            if isinstance(node, _SCOPE_NODES):
                return
        for child in ast.iter_child_nodes(node):
            visit(child, is_root=False)

    visit(scope, is_root=True)
    return sorted(
        collected, key=lambda n: (getattr(n, "lineno", 0), getattr(n, "col_offset", 0))
    )


def _stale_github_patch_targets() -> list[str]:
    stale_targets: list[str] = []
    for path in TESTS_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            is_patch_call = (
                isinstance(node.func, ast.Name) and node.func.id == "patch"
            ) or (isinstance(node.func, ast.Attribute) and node.func.attr == "patch")
            if not is_patch_call:
                continue
            target = node.args[0]
            if (
                isinstance(target, ast.Constant)
                and isinstance(target.value, str)
                and target.value.startswith("orchestune.github")
            ):
                stale_targets.append(f"{path.name}:{node.lineno}:{target.value}")
    return stale_targets


def _dotted_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _dotted_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return None


def _mock_patch_references(tree: ast.AST) -> tuple[set[str], set[str]]:
    patch_references: set[str] = set()
    github_forge_names = {"GitHubForge"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "unittest.mock":
            patch_references.update(
                alias.asname or alias.name
                for alias in node.names
                if alias.name == "patch"
            )
        elif isinstance(node, ast.ImportFrom) and node.module == "unittest":
            patch_references.update(
                f"{alias.asname or alias.name}.patch"
                for alias in node.names
                if alias.name == "mock"
            )
        elif isinstance(node, ast.Import):
            patch_references.update(
                f"{alias.asname or alias.name}.patch"
                for alias in node.names
                if alias.name == "unittest.mock"
            )
            patch_references.update(
                f"{alias.asname or alias.name}.mock.patch"
                for alias in node.names
                if alias.name == "unittest"
            )
        elif isinstance(node, ast.ImportFrom) and (
            node.module == "orchestune"
            or (node.module is not None and node.module.startswith("orchestune.forge"))
        ):
            github_forge_names.update(
                alias.asname or alias.name
                for alias in node.names
                if alias.name == "GitHubForge"
            )
    return patch_references, github_forge_names


def _string_scope_bindings(scope: ast.AST) -> tuple[set[str], dict[str, set[str]]]:
    assigned: set[str] = set()
    rebound_outer: set[str] = set()
    values: dict[str, set[str]] = defaultdict(set)
    if isinstance(scope, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
        arguments = scope.args
        assigned.update(
            argument.arg
            for argument in (
                *arguments.posonlyargs,
                *arguments.args,
                *arguments.kwonlyargs,
            )
        )
        assigned.update(
            argument.arg
            for argument in (arguments.vararg, arguments.kwarg)
            if argument is not None
        )
    for node in _nodes_in_scope(scope):
        if isinstance(node, _SCOPE_NODES):
            continue
        if isinstance(node, ast.Global | ast.Nonlocal):
            rebound_outer.update(node.names)
            continue
        targets, value = _assigned_names(node)
        for target in targets:
            if isinstance(target, ast.Name):
                assigned.add(target.id)
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    values[target.id].add(value.value)
        if isinstance(node, ast.For) and isinstance(node.target, ast.Name):
            assigned.add(node.target.id)
            if isinstance(node.iter, ast.List | ast.Tuple | ast.Set):
                values[node.target.id].update(
                    item.value
                    for item in node.iter.elts
                    if isinstance(item, ast.Constant) and isinstance(item.value, str)
                )
    return assigned - rebound_outer, dict(values)


def _call_argument(node: ast.Call, position: int, keyword: str) -> ast.expr | None:
    if len(node.args) > position:
        return node.args[position]
    return next(
        (item.value for item in node.keywords if item.arg == keyword),
        None,
    )


def _is_github_forge_target(target: ast.expr | None, names: set[str]) -> bool:
    if isinstance(target, ast.Constant) and isinstance(target.value, str):
        return bool(_GITHUB_FORGE_PATCH_TARGET.search(target.value))
    target_name = _dotted_name(target) if target else None
    return bool(target_name and target_name.rsplit(".", 1)[-1] in names)


def _is_github_forge_patch_call(
    node: ast.Call,
    patch_references: set[str],
    github_forge_names: set[str],
    bound_strings: dict[str, set[str]],
) -> bool:
    function_name = _dotted_name(node.func)
    if function_name in patch_references:
        target = _call_argument(node, 0, "target")
        target_values = (
            {target.value}
            if isinstance(target, ast.Constant) and isinstance(target.value, str)
            else bound_strings.get(target.id, set())
            if isinstance(target, ast.Name)
            else set()
        )
        return any(_GITHUB_FORGE_PATCH_TARGET.search(value) for value in target_values)
    if function_name in {
        f"{ref}.{method}"
        for ref in patch_references
        for method in ("object", "multiple")
    }:
        return _is_github_forge_target(
            _call_argument(node, 0, "target"), github_forge_names
        )
    return False


def _scan_github_forge_patch_scope(
    scope: ast.AST,
    inherited: dict[str, set[str]],
    patch_references: set[str],
    github_forge_names: set[str],
    violations: list[int],
) -> None:
    assigned, candidates = _string_scope_bindings(scope)
    bindings = {
        name: set(values) for name, values in inherited.items() if name not in assigned
    }
    for name, values in candidates.items():
        bindings[name] = bindings.get(name, set()) | values

    for node in _nodes_in_scope(scope):
        if isinstance(node, _SCOPE_NODES):
            nested = inherited if isinstance(scope, ast.ClassDef) else bindings
            _scan_github_forge_patch_scope(
                node,
                nested,
                patch_references,
                github_forge_names,
                violations,
            )
        elif isinstance(node, ast.Call) and _is_github_forge_patch_call(
            node, patch_references, github_forge_names, bindings
        ):
            violations.append(node.lineno)


def _github_forge_patch_lines(source: str) -> list[int]:
    tree = ast.parse(source)
    patch_references, github_forge_names = _mock_patch_references(tree)
    violations: list[int] = []
    _scan_github_forge_patch_scope(
        tree, {}, patch_references, github_forge_names, violations
    )
    return sorted(violations)


def _github_forge_patch_violations() -> list[str]:
    violations: list[str] = []
    for path in sorted(TESTS_ROOT.rglob("*.py")):
        relative_path = path.relative_to(TESTS_ROOT).as_posix()
        if relative_path in GITHUB_FORGE_PATCH_EXEMPTIONS:
            continue
        for line in _github_forge_patch_lines(path.read_text(encoding="utf-8")):
            violations.append(f"{path.relative_to(REPO_ROOT)}:{line}")
    return violations


def test_github_compatibility_module_is_removed() -> None:
    assert not (PACKAGE_ROOT / "github.py").exists()


def test_tests_do_not_patch_removed_github_module() -> None:
    assert _stale_github_patch_targets() == []


def test_tests_do_not_patch_github_forge() -> None:
    assert _github_forge_patch_violations() == []


def test_github_forge_patch_detector_flags_aliased_direct_patch() -> None:
    source = """
from unittest.mock import patch as replace

with replace("orchestune.forge.GitHubForge.list_prs"):
    pass
"""

    assert _github_forge_patch_lines(source) == [4]


def test_github_forge_patch_detector_flags_patch_object_alias() -> None:
    source = """
from unittest.mock import patch
from orchestune.forge import GitHubForge as ProductionForge

with patch.object(ProductionForge, "list_prs"):
    pass
"""

    assert _github_forge_patch_lines(source) == [5]


def test_github_forge_patch_detector_resolves_loop_target() -> None:
    source = """
from unittest.mock import patch
for target in (
    "orchestune.dispatch_gc.is_process_alive",
    "orchestune.forge.GitHubForge.list_prs",
):
    with patch(target):
        pass
"""

    assert _github_forge_patch_lines(source) == [7]


def test_github_forge_patch_detector_flags_keyword_targets() -> None:
    source = """
from unittest.mock import patch
from orchestune.forge import GitHubForge

with patch(target="orchestune.forge.GitHubForge.list_prs"):
    pass
with patch.object(target=GitHubForge, attribute="list_prs"):
    pass
"""

    assert _github_forge_patch_lines(source) == [5, 7]


def test_github_forge_patch_detector_flags_unittest_import() -> None:
    source = """
import unittest

with unittest.mock.patch("orchestune.forge.GitHubForge.list_prs"):
    pass
"""

    assert _github_forge_patch_lines(source) == [4]


def test_github_forge_patch_detector_flags_public_import_alias() -> None:
    source = """
from unittest.mock import patch
from orchestune import GitHubForge as ProductionForge

with patch.object(ProductionForge, "list_prs"):
    pass
"""

    assert _github_forge_patch_lines(source) == [5]


def test_github_forge_patch_detector_flags_patch_multiple() -> None:
    source = """
from unittest.mock import DEFAULT, patch
from orchestune.forge import GitHubForge

with patch.multiple(target=GitHubForge, list_prs=DEFAULT):
    pass
"""

    assert _github_forge_patch_lines(source) == [5]


def test_github_forge_patch_detector_keeps_variable_bindings_scoped() -> None:
    source = """
from unittest.mock import patch

def forge_helper():
    target = "orchestune.forge.GitHubForge.list_prs"
    return target

def harmless_helper():
    target = "orchestune.dispatch_gc.is_process_alive"
    with patch(target):
        pass
"""

    assert _github_forge_patch_lines(source) == []


def test_github_forge_patch_detector_treats_parameters_as_local() -> None:
    source = """
from unittest.mock import patch

target = "orchestune.forge.GitHubForge.list_prs"

def helper(target):
    with patch(target):
        pass

lambda target: patch(target)
"""

    assert _github_forge_patch_lines(source) == []


def test_github_forge_patch_detector_flags_subpackage_import() -> None:
    """#614: orchestune.forge.issues 等のサブパッケージからの GitHubForge import も patch.object 検出の対象になること。"""
    source = """
from unittest.mock import patch
from orchestune.forge.issues import GitHubForge as SubForge

with patch.object(SubForge, "list_prs"):
    pass
"""
    assert _github_forge_patch_lines(source) == [5]
