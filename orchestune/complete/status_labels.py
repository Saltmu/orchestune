"""Live completion-label reconciliation under the caller's generation lock."""

from __future__ import annotations

from collections.abc import Callable, Iterable

from orchestune.complete.contracts import (
    CompleteFailureReason,
    CompletionLabelStatus,
    CompletionLabelTransitionResult,
)
from orchestune.forge import Forge
from orchestune.labels import StatusLabel
from orchestune.ledger.status_labels import PRIMARY_STATUS_LABELS

__all__ = ["transition_completion_status_label"]


# Completion never deletes protective, auxiliary, or unknown status labels.
_COMPLETION_TARGETS = frozenset(
    {StatusLabel.DONE, StatusLabel.BLOCKED, StatusLabel.NOT_NEEDED}
)


def _completion_result(
    status: CompletionLabelStatus,
    target: str,
    labels: Iterable[str] = (),
    operation: str | None = None,
) -> CompletionLabelTransitionResult:
    reasons = {
        CompletionLabelStatus.ADD_FAILED: CompleteFailureReason.LABEL_ADD_FAILED,
        CompletionLabelStatus.CLEANUP_INCOMPLETE: CompleteFailureReason.LABEL_CLEANUP_INCOMPLETE,
        CompletionLabelStatus.UNKNOWN: CompleteFailureReason.LABEL_STATE_UNKNOWN,
        CompletionLabelStatus.CONFLICT: CompleteFailureReason.LABEL_CONFLICT,
    }
    return CompletionLabelTransitionResult(
        status, target, tuple(sorted(set(labels))), operation, reasons.get(status)
    )


def _completion_observe(
    forge: Forge,
    issue_number: int | str,
    target: str,
    generation_matches: Callable[[], bool],
) -> tuple[str, ...] | CompletionLabelTransitionResult:
    try:
        labels = tuple(forge.get_issue_labels(issue_number))
        current = generation_matches()
    except Exception:
        return _completion_result(
            CompletionLabelStatus.UNKNOWN, target, operation="get"
        )
    allowed = {*PRIMARY_STATUS_LABELS, target, StatusLabel.FORCE_SERIAL}
    conflict = any(
        label.startswith("status:") and label not in allowed for label in labels
    )
    if not current or conflict:
        return _completion_result(
            CompletionLabelStatus.CONFLICT, target, labels, "reconcile"
        )
    return labels


def _completion_mutate(
    forge: Forge,
    issue_number: int | str,
    target: str,
    generation_matches: Callable[[], bool],
    action: str,
    label: str,
) -> tuple[str, ...] | CompletionLabelTransitionResult:
    # An API response is not evidence of the resulting live state. Always read back.
    try:
        if action == "add":
            forge.add_label(issue_number, label)
        else:
            forge.remove_label(issue_number, label)
    except Exception:
        pass
    return _completion_observe(forge, issue_number, target, generation_matches)


def transition_completion_status_label(
    forge: Forge,
    issue_number: int | str,
    target_label: str,
    *,
    generation_matches: Callable[[], bool],
) -> CompletionLabelTransitionResult:
    """Reconcile completion labels under the caller's lock and valid reservation.

    The required callback verifies the caller's generation/reservation at every
    observation and must raise when verification is unavailable. This helper
    does not provide ownership or distributed exclusion. Each write is attempted
    once; an unsuccessful response is recovered only through live evidence.
    """
    if target_label not in _COMPLETION_TARGETS:
        raise ValueError("completion target must be done, blocked, or not-needed")
    labels = _completion_observe(forge, issue_number, target_label, generation_matches)
    if isinstance(labels, CompletionLabelTransitionResult):
        return labels
    if target_label not in labels:
        labels = _completion_mutate(
            forge, issue_number, target_label, generation_matches, "add", target_label
        )
        if isinstance(labels, CompletionLabelTransitionResult):
            return labels
        if target_label not in labels:
            return _completion_result(
                CompletionLabelStatus.ADD_FAILED,
                target_label,
                labels,
                f"add:{target_label}",
            )
    return _completion_cleanup(
        forge, issue_number, target_label, generation_matches, labels
    )


def _completion_cleanup(
    forge: Forge,
    issue_number: int | str,
    target_label: str,
    generation_matches: Callable[[], bool],
    labels: tuple[str, ...],
) -> CompletionLabelTransitionResult:
    observed: tuple[str, ...] | CompletionLabelTransitionResult = labels
    for old in PRIMARY_STATUS_LABELS:
        if old == target_label or old not in labels:
            continue
        observed = _completion_observe(
            forge, issue_number, target_label, generation_matches
        )
        if isinstance(observed, CompletionLabelTransitionResult):
            return observed
        labels = observed
        if target_label not in labels:
            return _completion_result(
                CompletionLabelStatus.UNKNOWN, target_label, labels, "reconcile"
            )
        if old not in labels:
            continue
        observed = _completion_mutate(
            forge, issue_number, target_label, generation_matches, "remove", old
        )
        if isinstance(observed, CompletionLabelTransitionResult):
            return observed
        labels = observed
        if old in labels:
            return _completion_result(
                CompletionLabelStatus.CLEANUP_INCOMPLETE,
                target_label,
                labels,
                f"remove:{old}",
            )
    return _completion_verify(forge, issue_number, target_label, generation_matches)


def _completion_verify(
    forge: Forge,
    issue_number: int | str,
    target: str,
    generation_matches: Callable[[], bool],
) -> CompletionLabelTransitionResult:
    labels = _completion_observe(forge, issue_number, target, generation_matches)
    if isinstance(labels, CompletionLabelTransitionResult):
        return labels
    if target not in labels:
        return _completion_result(CompletionLabelStatus.UNKNOWN, target, labels, "get")
    if any(old != target and old in labels for old in PRIMARY_STATUS_LABELS):
        return _completion_result(
            CompletionLabelStatus.CLEANUP_INCOMPLETE, target, labels, "reconcile"
        )
    return _completion_result(CompletionLabelStatus.CONFIRMED, target, labels)
