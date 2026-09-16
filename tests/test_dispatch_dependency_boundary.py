from __future__ import annotations

import ast
import tomllib
from pathlib import Path

from dependency_boundary_test_support import (
    BoundaryException,
    boundary_violations,
    typing_escape_names,
)

REPO_ROOT = Path(__file__).parents[1]


def test_raw_dependency_and_removed_context_attributes_are_rejected() -> None:
    source = """
from orchestune.models import Task

def policy(task, ctx):
    return task.depends_on, task.native_depends_on, ctx.run_state
"""

    violations = boundary_violations(source, module="orchestune.dispatch.policy")

    assert {(item.function, item.attribute, item.kind) for item in violations} == {
        ("<module>", "Task", "raw-task-import"),
        ("policy", "depends_on", "raw-attribute"),
        ("policy", "native_depends_on", "raw-attribute"),
        ("policy", "run_state", "removed-context-attribute"),
    }


def test_qualified_raw_task_module_imports_are_rejected() -> None:
    sources = (
        """
import orchestune.models as models

def policy(task: models.Task):
    return task.issue_number
""",
        """
from orchestune import models

def policy(task: models.Task):
    return task.issue_number
""",
        """
from orchestune.dispatch import scoring

def policy(task: scoring.Task):
    return task.issue_number
""",
    )

    for source in sources:
        violations = boundary_violations(source, module="orchestune.dispatch.policy")
        assert {(item.attribute, item.kind) for item in violations} == {
            ("Task", "raw-task-import")
        }


def test_reexported_and_relative_raw_task_imports_are_rejected() -> None:
    sources = (
        "from orchestune import Task",
        "from ..models import Task",
        "from .scoring import Task",
        "from .. import models",
    )

    for source in sources:
        violations = boundary_violations(source, module="orchestune.dispatch.policy")
        assert {(item.attribute, item.kind) for item in violations} == {
            ("Task", "raw-task-import")
        }


def test_package_initializer_relative_raw_task_import_is_rejected() -> None:
    violations = boundary_violations(
        "from .scoring import Task",
        module="orchestune.dispatch",
        is_package=True,
    )

    assert {(item.attribute, item.kind) for item in violations} == {
        ("Task", "raw-task-import")
    }


def test_literal_getattr_cannot_bypass_the_boundary() -> None:
    source = """
def policy(task, ctx):
    return getattr(task, "depends_on"), getattr(ctx, "tasks_by_issue")
"""

    violations = boundary_violations(source, module="orchestune.dispatch.policy")

    assert {(item.attribute, item.kind) for item in violations} == {
        ("depends_on", "literal-getattr"),
        ("tasks_by_issue", "literal-getattr"),
    }


def test_comments_strings_and_unrelated_attributes_are_not_flagged() -> None:
    source = '''
"""task.depends_on and ctx.run_state are documentation only."""
# getattr(task, "native_depends_on") is a comment.
def policy(task):
    marker = "ctx.tasks_by_issue"
    return task.issue_number, marker
'''

    assert not boundary_violations(source, module="orchestune.dispatch.policy")


def test_exact_reasoned_exception_allows_semantic_subtask_dependency() -> None:
    source = """
def rank(subtask):
    return subtask.depends_on
"""
    exception = BoundaryException(
        module="orchestune.dispatch.rank",
        function="rank",
        attribute="depends_on",
        reason="SubTask carries the derived semantic DAG edge.",
    )

    assert not boundary_violations(
        source,
        module="orchestune.dispatch.rank",
        exceptions=frozenset({exception}),
    )
    assert boundary_violations(
        source,
        module="orchestune.dispatch.other_rank",
        exceptions=frozenset({exception}),
    )

    evasive_source = """
def rank(subtask):
    return getattr(subtask, "depends_on")
"""
    assert boundary_violations(
        evasive_source,
        module="orchestune.dispatch.rank",
        exceptions=frozenset({exception}),
    )


def test_exception_requires_a_reason() -> None:
    try:
        BoundaryException(
            module="orchestune.dispatch.rank",
            function="rank",
            attribute="depends_on",
            reason="",
        )
    except ValueError as exc:
        assert "reason" in str(exc)
    else:
        raise AssertionError("empty exception reasons must be rejected")


def test_metadata_and_policy_protocols_declare_no_raw_fields_or_escape_types() -> None:
    paths = (
        REPO_ROOT / "orchestune" / "task_metadata.py",
        REPO_ROOT / "orchestune" / "dispatch" / "dependency_policy.py",
        REPO_ROOT / "orchestune" / "dispatch" / "status_dependency_policy.py",
        REPO_ROOT / "orchestune" / "dispatch" / "cycle_action_contracts.py",
    )
    raw_names = {"depends_on", "native_depends_on"}

    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        declarations = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
        }
        declarations.update(
            node.target.id
            for node in ast.walk(tree)
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
        )
        assert declarations.isdisjoint(raw_names), path
        assert not typing_escape_names(path.read_text(encoding="utf-8")), path


def test_typing_escape_detector_tracks_aliases_without_text_false_positives() -> None:
    source = """
import typing as t
from typing import Any as Dynamic
from typing import cast as force_type

DOCUMENTATION = "typing.Any and t.cast are only text here"

def policy(value: t.Any):
    return t.cast(str, value), Dynamic, force_type
"""

    assert typing_escape_names(source) == frozenset({"Any", "cast"})
    assert not typing_escape_names('MARKER = "typing.Any and t.cast"')


def test_ruff_enables_slf001_with_tests_only_ignore() -> None:
    config = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    lint = config["tool"]["ruff"]["lint"]

    assert "SLF001" in lint["select"]
    assert lint["per-file-ignores"] == {"tests/**/*.py": ["SLF001"]}


def test_argparse_private_registry_has_a_line_local_reasoned_noqa() -> None:
    source = (REPO_ROOT / "orchestune" / "dispatch" / "dispatcher.py").read_text(
        encoding="utf-8"
    )
    action_line = next(
        line for line in source.splitlines() if "parser._actions" in line
    )

    assert "# noqa: SLF001" in action_line
    assert "argparse" in action_line
