"""Recoverable child registration and native relationship operations."""

from pathlib import Path
from typing import Any

from orchestune.forge import RelationshipUnavailableError
from orchestune.ledger.status_labels import transition_status_label
from orchestune.ledger.status_machine import lifecycle_labels
from orchestune.plan_writer import write_issue_numbers
from orchestune.provisioning.rendering import derive_subtask_labels, subtask_issue_title
from orchestune.remediation.body import validate_registered_body
from orchestune.remediation.models import RemediationTask

_DEGRADED = (RelationshipUnavailableError, AttributeError, NotImplementedError)


def ensure_initial_label(
    forge: Any, number: int, task: RemediationTask, *, dependencies_done: bool = False
) -> None:
    labels = forge.get_issue_labels(number)
    if not lifecycle_labels(labels):
        target = derive_subtask_labels(
            task.subtask, dependencies_done=dependencies_done
        )[0]
        transition_status_label(forge, number, target, ())


def register_child(
    forge: Any,
    task: RemediationTask,
    parent: int,
    body: str,
    number: int | None,
    spec_path: str | Path,
    *,
    dependencies_done: bool = False,
) -> int:
    if number is None:
        number = forge.create_issue(
            subtask_issue_title(task.subtask),
            body,
            labels=derive_subtask_labels(
                task.subtask, dependencies_done=dependencies_done
            ),
        )
        # Persist identity before any other operation, including readback.
        write_issue_numbers(spec_path, {task.subtask.id: number})
    else:
        current = forge.get_issue(number)
        if current is None:
            raise ValueError(f"Issue #{number} disappeared")
        if current.body != body:
            forge.update_issue_body(number, body)
        write_issue_numbers(spec_path, {task.subtask.id: number})
    written = forge.get_issue(number)
    if written is None:
        raise ValueError(f"Issue #{number} readback missing")
    validate_registered_body(written, task, parent)
    ensure_initial_label(forge, number, task, dependencies_done=dependencies_done)
    return number


def link_child(
    forge: Any, parent: int, number: int, dependencies: tuple[int, ...]
) -> None:
    issue = forge.get_issue(number)
    if issue is None:
        raise ValueError(f"Issue #{number} disappeared")
    if (issue.parent or {}).get("number") != parent:
        try:
            forge.add_sub_issue(parent, number)
        except _DEGRADED:
            pass  # parent_issue_number in Footprint is the discovery fallback.
    for dependency in dependencies:
        if dependency not in issue.blocked_by:
            try:
                forge.set_blocked_by(number, dependency)
            except _DEGRADED:
                pass  # depends_on in Footprint remains authoritative metadata.
