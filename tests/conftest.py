"""Shared test factories and default test isolation fixtures."""

from __future__ import annotations

import contextlib
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from orchestune.dispatch_scoring import Task
from orchestune.forge import BootstrapResult, Forge
from orchestune.github import IssueRecord, PrRecord


def make_task(issue_number: int = 1, **overrides: Any) -> Task:
    """Create a Task with valid defaults; override only fields relevant to a test."""
    values: dict[str, Any] = {
        "issue_number": issue_number,
        "subtask_id": f"task-{issue_number}",
        "footprint": ("src/foo.py",),
        "symbols": (),
        "risk": False,
        "priority": "medium",
        "progress_partial": False,
        "status_labels": ("status:queued",),
        "created_at": "2026-01-01T00:00:00+00:00",
        "depends_on": (),
    }
    values.update(overrides)
    return Task(**values)


def make_issue(number: int = 1, **overrides: Any) -> IssueRecord:
    """Create an IssueRecord containing a valid Footprint YAML block by default."""
    footprint = tuple(overrides.pop("footprint", ("src/foo.py",)))
    symbols = tuple(overrides.pop("symbols", ("foo.Foo",)))
    depends_on = tuple(overrides.pop("depends_on", ()))
    subtask_id = overrides.pop("subtask_id", f"task-{number}")
    footprint_lines = "\n".join(f"  - {item}" for item in footprint) or "  []"
    symbols_lines = "\n".join(f"  - {item}" for item in symbols) or "  []"
    depends_on_lines = "\n".join(f"  - {item}" for item in depends_on) or "  []"
    body = overrides.pop(
        "body",
        "\n".join(
            (
                "## Footprint",
                "```yaml",
                f"subtask_id: {subtask_id}",
                "footprint:",
                footprint_lines,
                "symbols:",
                symbols_lines,
                "depends_on:",
                depends_on_lines,
                "```",
                "",
            )
        ),
    )
    values: dict[str, Any] = {
        "number": number,
        "title": f"Test Issue {number}",
        "body": body,
        "labels": ("status:queued",),
        "created_at": "2026-01-01T00:00:00+00:00",
        "parent": {"number": 100},
    }
    values.update(overrides)
    return IssueRecord(**values)


def make_pr(number: int = 1, **overrides: Any) -> PrRecord:
    """Create a PrRecord with a conventional issue branch by default."""
    values: dict[str, Any] = {
        "number": number,
        "head_ref": f"claude/issue-{number}-task-{number}",
        "changed_files": (),
    }
    values.update(overrides)
    return PrRecord(**values)


@pytest.fixture
def fake_forge() -> MagicMock:
    """A configurable Forge double for tests that are migrated off GitHubForge."""
    forge = MagicMock(spec=Forge)
    forge.check_auth.return_value = None
    forge.ensure_labels.return_value = BootstrapResult((), ())
    return forge


@pytest.fixture(autouse=True)
def _stub_file_lock_by_default(request: pytest.FixtureRequest):
    """Avoid process-wide fcntl contention except in lock-specific tests."""
    if request.node.get_closest_marker("uses_real_file_lock") is not None:
        yield
        return
    with (
        patch(
            "orchestune.dispatcher.file_lock",
            lambda _lock_path: contextlib.nullcontext(),
        ),
        patch(
            "orchestune.integrator.file_lock",
            lambda _lock_path: contextlib.nullcontext(),
        ),
    ):
        yield
