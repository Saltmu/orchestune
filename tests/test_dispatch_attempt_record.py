"""Journal parsing and preservation contracts for #818."""

import json
from dataclasses import asdict, replace

import pytest

from orchestune.dispatch.attempt_record import (
    MARKER,
    LaunchAttempt,
    attempt_from_body,
    read_attempt,
    write_attempt,
)
from tests.conftest import FakeForge, make_issue


def _attempt():
    return LaunchAttempt(
        "attempt-1",
        "prepared",
        "codex-cloud",
        "claude/issue-1-task-1",
        "origin/main",
        100.0,
    )


def test_preserves_issue_prose_and_roundtrips_crlf():
    forge = FakeForge()
    forge.issues[1] = make_issue(body="Task\r\n\r\n```yaml\nsubtask_id: task-1\n```\n")
    attempt = _attempt()
    write_attempt(forge, 1, attempt, expected=None)
    assert read_attempt(forge, 1) == attempt
    assert "subtask_id: task-1" in forge.issues[1].body
    updated = replace(attempt, phase="unknown")
    write_attempt(forge, 1, updated, expected=attempt)
    assert attempt_from_body(forge.issues[1].body.replace("\n", "\r\n")) == updated
    assert forge.issues[1].body.count(MARKER) == 1
    with pytest.raises(ValueError, match="changed"):
        write_attempt(forge, 1, attempt, expected=attempt)
    assert read_attempt(forge, 1) == updated


@pytest.mark.parametrize(
    "body",
    [
        MARKER,
        MARKER + "\n```json\n[]\n```",
        MARKER + "\n```json\n{}\n```",
        MARKER + MARKER,
    ],
)
def test_incomplete_or_duplicate_journal_fails_closed(body):
    with pytest.raises(ValueError):
        attempt_from_body(body)


@pytest.mark.parametrize(
    "override",
    [
        {"phase": "typo"},
        {"phase": "launched"},
        {"started_at": True},
        {"started_at": float("nan")},
        {"external_id": 123},
        {"external_url": ""},
        {"branch": "../../escape"},
        {"unexpected_field": 1},
        {"attempt_id": ""},
    ],
)
def test_invalid_journal_fields_fail_closed(override):
    data = asdict(_attempt()) | override
    body = MARKER + "\n```json\n" + json.dumps(data) + "\n```"
    with pytest.raises(ValueError):
        attempt_from_body(body)


def test_absent_journal_and_unavailable_issue_are_distinct():
    assert attempt_from_body("ordinary issue") is None
    with pytest.raises(ValueError, match="cannot read"):
        read_attempt(FakeForge(), 1)
