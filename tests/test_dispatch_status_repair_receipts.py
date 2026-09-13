"""Issue #872: verified status-transition evidence is emitted fail-closed."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, dataclass
from unittest.mock import patch

import pytest

from orchestune.consistency.intents import IntentJournal
from orchestune.consistency.models import RepairStatus
from orchestune.dispatch.status_repair import (
    VerifiedStatusTransition,
    execute_status_repair_command,
)
from orchestune.labels import StatusLabel
from tests.conftest import make_issue, make_task
from tests.test_consistency_status_repair import _config, _plan


@dataclass(frozen=True)
class _CompletionEvidence:
    confirmed: frozenset[int] = frozenset()

    def is_completion_confirmed(self, issue_number: int) -> bool:
        return issue_number in self.confirmed


def _remove_done_command(task):
    return _plan({task.issue_number: task})[1][0]


def _execute(command, tasks, config, callback=None):
    return execute_status_repair_command(
        command,
        tasks,
        completion_evidence=_CompletionEvidence(),
        config=config,
        on_verified=callback,
    )


def test_verified_transition_is_an_immutable_value():
    transition = VerifiedStatusTransition(1, ("before",), ("after",), "intent")

    with pytest.raises(FrozenInstanceError):
        transition.intent_id = "changed"  # type: ignore[misc]


def test_applied_callback_receives_full_before_and_verified_labels(
    tmp_path, in_memory_forge
):
    before = (StatusLabel.DONE, StatusLabel.QUEUED, "priority:high")
    task = make_task(1, status_labels=before)
    in_memory_forge.seed_issue(make_issue(1, labels=before))
    received = []

    result = _execute(
        _remove_done_command(task),
        {1: task},
        _config(tmp_path, in_memory_forge),
        received.append,
    )

    assert result.status is RepairStatus.APPLIED
    assert len(received) == 1
    evidence = received[0]
    assert evidence.issue_number == 1
    assert evidence.before_labels == before
    assert evidence.verified_labels == (StatusLabel.QUEUED, "priority:high")
    assert evidence.intent_id.startswith("status-1-")


def test_dry_run_does_not_emit_verified_transition(tmp_path, in_memory_forge):
    labels = (StatusLabel.DONE, StatusLabel.QUEUED)
    task = make_task(1, status_labels=labels)
    received = []

    result = _execute(
        _remove_done_command(task),
        {1: task},
        _config(tmp_path, in_memory_forge, apply=False),
        received.append,
    )

    assert result.status is RepairStatus.SKIPPED
    assert received == []


def test_failed_forge_mutation_does_not_emit_verified_transition(
    tmp_path, in_memory_forge
):
    labels = (StatusLabel.DONE, StatusLabel.QUEUED)
    task = make_task(1, status_labels=labels)
    in_memory_forge.seed_issue(make_issue(1, labels=labels))
    received = []

    with patch.object(
        in_memory_forge, "remove_label", side_effect=RuntimeError("Forge down")
    ):
        result = _execute(
            _remove_done_command(task),
            {1: task},
            _config(tmp_path, in_memory_forge),
            received.append,
        )

    assert result.status is RepairStatus.FAILED
    assert received == []


def test_live_verification_failure_does_not_emit_verified_transition(
    tmp_path, in_memory_forge
):
    labels = (StatusLabel.DONE, StatusLabel.QUEUED)
    task = make_task(1, status_labels=labels)
    in_memory_forge.seed_issue(make_issue(1, labels=labels))
    received = []

    with patch.object(
        in_memory_forge, "get_issue_labels", return_value=(StatusLabel.DONE,)
    ):
        result = _execute(
            _remove_done_command(task),
            {1: task},
            _config(tmp_path, in_memory_forge),
            received.append,
        )

    assert result.status is RepairStatus.SKIPPED
    assert received == []


def test_stale_precondition_does_not_emit_verified_transition(
    tmp_path, in_memory_forge
):
    labels = (StatusLabel.DONE, StatusLabel.QUEUED)
    task = make_task(1, status_labels=labels)
    in_memory_forge.seed_issue(make_issue(1, labels=(StatusLabel.DONE,)))
    received = []

    result = _execute(
        _remove_done_command(task),
        {1: task},
        _config(tmp_path, in_memory_forge),
        received.append,
    )

    assert result.status is RepairStatus.SKIPPED
    assert received == []


def test_callback_failure_returns_failed_after_verified_forge_state(
    tmp_path, in_memory_forge
):
    labels = (StatusLabel.DONE, StatusLabel.QUEUED)
    task = make_task(1, status_labels=labels)
    in_memory_forge.seed_issue(make_issue(1, labels=labels))

    def fail(_transition):
        raise RuntimeError("Context rejected receipt")

    result = _execute(
        _remove_done_command(task),
        {1: task},
        _config(tmp_path, in_memory_forge),
        fail,
    )

    assert result.status is RepairStatus.FAILED
    assert "Context rejected receipt" in result.diagnostics[0]
    assert in_memory_forge.get_issue_labels(1) == (StatusLabel.QUEUED,)


def test_journal_verification_failure_does_not_emit_transition(
    tmp_path, in_memory_forge
):
    labels = (StatusLabel.DONE, StatusLabel.QUEUED)
    task = make_task(1, status_labels=labels)
    in_memory_forge.seed_issue(make_issue(1, labels=labels))
    received = []

    with patch.object(
        IntentJournal,
        "mark_verified",
        autospec=True,
        side_effect=OSError("journal verification failed"),
    ):
        result = _execute(
            _remove_done_command(task),
            {1: task},
            _config(tmp_path, in_memory_forge),
            received.append,
        )

    assert result.status is RepairStatus.FAILED
    assert received == []
    assert in_memory_forge.get_issue_labels(1) == (StatusLabel.QUEUED,)
