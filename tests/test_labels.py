"""Tests for status label constants and AST-based direct literal CI enforcement."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from orchestune import StatusLabel
from orchestune.forge.admin import REQUIRED_LABELS
from orchestune.labels import (
    ALL_STATUS_LABELS,
    STATUS_LABEL_PREFIX,
)

PACKAGE_ROOT = Path(__file__).parents[1] / "orchestune"

_STATUS_LABEL_LITERAL_PATTERN = re.compile(r"^status:[a-zA-Z0-9_-]+$")
_EXEMPT_FILES = frozenset({"labels.py"})


def _find_status_literals_in_ast(
    tree: ast.AST, filename: str
) -> list[tuple[int, int, str]]:
    """Scan an AST for string constants that match `status:*` label literals."""
    violations: list[tuple[int, int, str]] = []
    # Collect all docstring nodes so we don't flag accidental docstrings
    docstring_nodes: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(
            node,
            ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef,
        ):
            if (
                node.body
                and isinstance(node.body[0], ast.Expr)
                and isinstance(node.body[0].value, ast.Constant)
                and isinstance(node.body[0].value.value, str)
            ):
                docstring_nodes.add(id(node.body[0].value))

    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in docstring_nodes:
                continue
            val = node.value
            if _STATUS_LABEL_LITERAL_PATTERN.match(val):
                violations.append((node.lineno, node.col_offset, val))
    return violations


def test_status_label_enum_values() -> None:
    """Verify all 10 StatusLabel Enum members and their string values."""
    assert StatusLabel.QUEUED == "status:queued"
    assert StatusLabel.BLOCKED == "status:blocked"
    assert StatusLabel.BLOCKED_RECOMPUTE == "status:blocked-recompute"
    assert StatusLabel.BLOCKED_HUMAN_REVIEW == "status:blocked-human-review"
    assert StatusLabel.DONE == "status:done"
    assert StatusLabel.EXTERNAL_LOCK == "status:external-lock"
    assert StatusLabel.FORCE_SERIAL == "status:force-serial"
    assert StatusLabel.IN_PROGRESS == "status:in-progress"
    assert StatusLabel.MANUAL_MERGE_REQUIRED == "status:manual-merge-required"
    assert StatusLabel.NOT_NEEDED == "status:not-needed"
    assert len(StatusLabel) == 10
    assert STATUS_LABEL_PREFIX == "status:"

    for label in StatusLabel:
        assert label.startswith(STATUS_LABEL_PREFIX)
        assert label in ALL_STATUS_LABELS


def test_all_status_labels_in_required_labels() -> None:
    """Verify all StatusLabel members exist in REQUIRED_LABELS."""
    required_names = {spec.name for spec in REQUIRED_LABELS}
    for label in StatusLabel:
        assert label.value in required_names


def test_no_direct_status_string_literals_in_production_code() -> None:
    """AST CI check: verify no direct `status:*` string literals in orchestune/*.py (except labels.py)."""
    violations_by_file: dict[str, list[tuple[int, int, str]]] = {}

    for py_file in sorted(PACKAGE_ROOT.rglob("*.py")):
        rel_path = py_file.relative_to(PACKAGE_ROOT)
        if rel_path.name in _EXEMPT_FILES:
            continue
        code = py_file.read_text(encoding="utf-8")
        tree = ast.parse(code, filename=str(py_file))
        file_violations = _find_status_literals_in_ast(tree, str(rel_path))
        if file_violations:
            violations_by_file[str(rel_path)] = file_violations

    if violations_by_file:
        details = "\n".join(
            f"  {file}:{line}:{col} -> literal {val!r}"
            for file, viols in violations_by_file.items()
            for line, col, val in viols
        )
        pytest.fail(
            f"Found direct `status:*` string literals in production code. "
            f"Use `StatusLabel` from `orchestune.labels` instead:\n{details}"
        )


def test_ast_detector_catches_violations() -> None:
    """Negative test: ensure AST detector correctly detects direct status:* literals."""
    bad_code = """
def sample():
    return "status:queued"
"""
    tree = ast.parse(bad_code)
    viols = _find_status_literals_in_ast(tree, "test.py")
    assert len(viols) == 1
    assert viols[0][2] == "status:queued"

    good_code = """
\"\"\"status:queued docstring is ignored\"\"\"
from orchestune import StatusLabel
def sample():
    return StatusLabel.QUEUED
"""
    tree_good = ast.parse(good_code)
    viols_good = _find_status_literals_in_ast(tree_good, "test.py")
    assert viols_good == []
