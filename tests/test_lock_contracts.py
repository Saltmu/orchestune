"""Tests for orchestune.lock_contracts shared DTOs/constants."""

from __future__ import annotations

from orchestune.lock_contracts import (
    KIND_BRANCH,
    KIND_PR,
    ExternalLockConflict,
    ExternalLockScanResult,
)
from orchestune.models import Task


def _task(issue_number: int) -> Task:
    return Task(
        issue_number=issue_number,
        subtask_id=f"task-{issue_number}",
        footprint=(),
        symbols=(),
        risk=False,
        priority="medium",
        progress_partial=False,
        status_labels=(),
        created_at="2023-01-01T00:00:00+00:00",
        depends_on=(),
    )


def test_kind_constants() -> None:
    assert KIND_BRANCH == "branch"
    assert KIND_PR == "pr"


def test_external_lock_conflict_defaults_and_frozen() -> None:
    conflict = ExternalLockConflict(kind=KIND_BRANCH, source="feature/x")
    assert conflict.kind == KIND_BRANCH
    assert conflict.source == "feature/x"
    assert conflict.files == ()

    with_files = ExternalLockConflict(
        kind=KIND_PR, source="#42", files=("a.py", "b.py")
    )
    assert with_files.files == ("a.py", "b.py")


def test_external_lock_scan_result_defaults() -> None:
    locked = _task(1)
    unlocked = _task(2)
    conflict = ExternalLockConflict(kind=KIND_BRANCH, source="feature/x")

    result = ExternalLockScanResult(
        to_lock=[locked],
        to_unlock=[unlocked],
        conflicts={1: (conflict,)},
    )
    assert result.to_lock == [locked]
    assert result.to_unlock == [unlocked]
    assert result.conflicts == {1: (conflict,)}

    bare = ExternalLockScanResult(to_lock=[], to_unlock=[])
    assert bare.conflicts == {}
